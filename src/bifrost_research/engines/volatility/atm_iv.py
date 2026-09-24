"""ATM IV daily compute → features.option_metric_atm_iv_daily.

Sources, per contract-day: ``features.option_iv_reconstructed_daily`` (vendor IV
projected from snapshots, plus the daily Brent pass); for contracts it lacks, Brent
solved here from the day's ``raw_market.option_daily`` bar, limited to strikes ATM
can use. Only when both are empty, ``raw_market.v_option_snapshot_with_stock``.
Solving in place rather than storing is what lets every symbol in the universe
carry ATM IV back to the first option bar (2024-09): stored, the per-contract rows
would be ~23M for two years, and ATM IV is their only reader.

Algorithm independently reimplemented from bifrost_api.research.iv_atm
(no bifrost-core / trade-api pip dependency).

Per (symbol, expiry): nearest strikes to spot; avg call+put IV when both exist; iv in (0, 10).
A strike more than ``ATM_MAX_MONEYNESS`` from spot is not at the money: an expiry whose
nearest priced strike is further out gets no row rather than a deep ITM/OTM contract's IV.
Recomputing a day replaces that day's rows for every symbol the source has rows for.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from bifrost_research.db.upsert import batch_upsert
from bifrost_research.engines.volatility.iv_solver import (
    DTE_MAX,
    DTE_MIN,
    _mid_from_ohlc,
    _right_lit,
    observed_near_session,
    solve_iv,
)

_COLS = (
    "symbol",
    "trade_date",
    "expiry",
    "atm_strike",
    "atm_iv",
    "underlying_price",
    "iv_source",
    "computed_at",
)

IV_SOURCE_SNAPSHOT = "snapshot"
IV_SOURCE_RECONSTRUCTED = "reconstructed"
IV_SOURCE = IV_SOURCE_SNAPSHOT  # back-compat

# 2026-09-23: June–July IV30 on DEV came from expiries whose only priced contract
# sat at 15 or 310 against a spot of 107–134 (PLTR 0.074 / 2.256 / 0.040 / 2.647).
ATM_MAX_MONEYNESS = 0.10


def _row_to_dict(row: Any, columns: Sequence[str]) -> Dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {columns[i]: row[i] for i in range(min(len(columns), len(row)))}


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()[:10]
    if not s:
        return None
    return date.fromisoformat(s)


def _valid_iv(value: Any) -> float | None:
    if value is None:
        return None
    try:
        iv_f = float(value)
    except (TypeError, ValueError):
        return None
    if not (0.0 < iv_f < 10.0):
        return None
    return iv_f


# The one "current IV": VRP's atm_iv_30d and the IV percentile's iv_current both
# read iv30_from_expiries. Inside 7 DTE the ATM IV is pin and event noise; past 90
# it is a different tenor. (Until 0.108.0 the percentile used the median over every
# expiry: PLTR 2026-09-22 read 0.564 there and 0.464 in VRP.)
IV30_MIN_DTE = 7
IV30_MAX_DTE = 90


def interpolate_iv_at_dte(points: Sequence[tuple[int, float]], *, target_dte: int = 30) -> float | None:
    """IV at ``target_dte`` from (dte, iv) points: linear between the two expiries
    that bracket it, the nearest one when only one side exists, None when empty."""
    clean = sorted((int(d), float(v)) for d, v in points)
    if not clean:
        return None
    below = [pt for pt in clean if pt[0] <= target_dte]
    above = [pt for pt in clean if pt[0] >= target_dte]
    if below and above:
        d0, v0 = below[-1]
        d1, v1 = above[0]
        if d1 == d0:
            return round(v0, 8)
        w = (target_dte - d0) / (d1 - d0)
        return round(v0 + (v1 - v0) * w, 8)
    nearest = min(clean, key=lambda pt: abs(pt[0] - target_dte))
    return round(nearest[1], 8)


def iv30_from_expiries(trade_date: date, expiry_ivs: Iterable[Tuple[Any, Any]]) -> float | None:
    """ATM IV at 30 DTE from one day's (expiry, atm_iv) pairs: interpolated between
    the expiries that bracket 30 days, the nearest when one-sided, None when no
    expiry lies within ``IV30_MIN_DTE``–``IV30_MAX_DTE`` days."""
    points: list[tuple[int, float]] = []
    for expiry, iv in expiry_ivs:
        exp = _as_date(expiry)
        iv_f = _valid_iv(iv)
        if exp is None or iv_f is None:
            continue
        dte = (exp - trade_date).days
        if IV30_MIN_DTE <= dte <= IV30_MAX_DTE:
            points.append((dte, iv_f))
    return interpolate_iv_at_dte(points, target_dte=30)


def atm_iv_from_side_items(
    items: List[Tuple[float, Optional[float], Optional[float], float]],
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Return (atm_iv, iv_call, iv_put, best_strike) using nearest strikes with IV."""
    if not items:
        return None, None, None, None
    items_sorted = sorted(items, key=lambda x: x[0])
    best_call: Optional[float] = None
    best_put: Optional[float] = None
    best_strike: Optional[float] = None
    for _dist, iv_c, iv_p, st in items_sorted:
        if iv_c is not None and best_call is None:
            best_call = iv_c
            if best_strike is None:
                best_strike = st
        if iv_p is not None and best_put is None:
            best_put = iv_p
            if best_strike is None:
                best_strike = st
        if best_call is not None and best_put is not None:
            break

    atm_iv: Optional[float] = None
    if best_call is not None and best_put is not None:
        atm_iv = (best_call + best_put) / 2.0
    elif best_call is not None:
        atm_iv = best_call
    elif best_put is not None:
        atm_iv = best_put
    return atm_iv, best_call, best_put, best_strike


def build_expiry_side_items(
    rows: Sequence[Mapping[str, Any]],
    spot: float,
    *,
    max_moneyness: float | None = None,
) -> List[Tuple[float, Optional[float], Optional[float], float]]:
    """(distance, iv_call, iv_put, strike) per priced strike; ``max_moneyness`` drops
    strikes further than that fraction of spot."""
    max_dist = max_moneyness * spot if max_moneyness is not None else None
    items: List[Tuple[float, Optional[float], Optional[float], float]] = []
    for r in rows:
        try:
            strike = float(r.get("strike"))
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        iv_f = _valid_iv(r.get("iv"))
        if iv_f is None:
            continue
        right = str(r.get("option_right") or "").strip().upper()
        dist = abs(strike - spot)
        if max_dist is not None and dist > max_dist:
            continue
        if right in ("C", "CALL"):
            items.append((dist, iv_f, None, strike))
        elif right in ("P", "PUT"):
            items.append((dist, None, iv_f, strike))
    return items


def representative_spot(rows: Sequence[Mapping[str, Any]]) -> float | None:
    spots: list[float] = []
    for r in rows:
        up = r.get("underlying_price")
        if up is None:
            continue
        try:
            v = float(up)
        except (TypeError, ValueError):
            continue
        if v > 0:
            spots.append(v)
    if not spots:
        return None
    return float(median(spots))


def fetch_reconstructed_iv_rows_for_date(
    conn: Any,
    trade_date: date,
    *,
    underlyings: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    cols = (
        "option_ticker",
        "underlying",
        "iv",
        "underlying_price",
        "expiry",
        "strike",
        "option_right",
    )
    syms = [str(s).strip().upper() for s in (underlyings or []) if str(s).strip()]
    base = """
        SELECT option_ticker, symbol AS underlying, iv, spot AS underlying_price,
               expiry, strike, option_right
        FROM features.option_iv_reconstructed_daily
        WHERE trade_date = %s
          AND iv IS NOT NULL AND iv > 0
          AND spot IS NOT NULL AND spot > 0
    """
    with conn.cursor() as cur:
        if syms:
            cur.execute(base + " AND symbol = ANY(%s)", (trade_date, syms))
        else:
            cur.execute(base, (trade_date,))
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []
    return [_row_to_dict(r, cols) for r in (raw or [])]


def fetch_snapshot_iv_rows_for_date(
    conn: Any,
    trade_date: date,
    *,
    underlyings: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    cols = (
        "option_ticker",
        "underlying",
        "iv",
        "underlying_price",
        "expiry",
        "strike",
        "option_right",
    )
    syms = [str(s).strip().upper() for s in (underlyings or []) if str(s).strip()]
    base_sql = f"""
        SELECT DISTINCT ON (v.option_ticker)
          v.option_ticker,
          v.underlying,
          v.iv,
          v.underlying_price,
          oc.expiry,
          oc.strike,
          oc.option_right
        FROM raw_market.v_option_snapshot_with_stock v
        INNER JOIN raw_market.option_contract oc
          ON oc.option_ticker = v.option_ticker
        WHERE DATE(timezone('America/New_York', v.snapshot_ts)) = %s
          AND v.iv IS NOT NULL
          AND v.underlying_price IS NOT NULL
          AND {observed_near_session("v")}
    """
    with conn.cursor() as cur:
        if syms:
            cur.execute(
                base_sql
                + """
                  AND v.underlying = ANY(%s)
                ORDER BY v.option_ticker, v.snapshot_ts DESC
                """,
                (trade_date, syms),
            )
        else:
            cur.execute(
                base_sql
                + """
                ORDER BY v.option_ticker, v.snapshot_ts DESC
                """,
                (trade_date,),
            )
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []
    return [_row_to_dict(r, cols) for r in (raw or [])]


def fetch_option_daily_brent_rows_for_date(
    conn: Any,
    trade_date: date,
    *,
    underlyings: Sequence[str] | None = None,
    exclude: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Brent IV from the day's option_daily bars for contracts ATM can use (within
    ``ATM_MAX_MONEYNESS`` of the close, ``DTE_MIN``–``DTE_MAX`` days) that are not in
    ``exclude`` — the contracts the reconstructed table already prices."""
    skip = {str(t) for t in exclude}
    syms = [str(s).strip().upper() for s in (underlyings or []) if str(s).strip()]
    sql = f"""
        SELECT o.option_ticker, o.underlying, o.expiry, o.strike, o.option_right,
               o.high, o.low, o.close, s.close AS spot
        FROM raw_market.option_daily o
        JOIN raw_market.stock_daily s ON s.symbol = o.underlying AND s.bar_date = o.bar_date
        WHERE o.bar_date = %s
          AND s.close > 0
          AND (o.expiry - o.bar_date) BETWEEN {DTE_MIN} AND {DTE_MAX}
          AND o.strike BETWEEN {1 - ATM_MAX_MONEYNESS} * s.close AND {1 + ATM_MAX_MONEYNESS} * s.close
    """
    with conn.cursor() as cur:
        if syms:
            cur.execute(sql + " AND o.underlying = ANY(%s)", (trade_date, syms))
        else:
            cur.execute(sql, (trade_date,))
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []
    out: list[dict[str, Any]] = []
    for ticker, und, expiry, strike, right_raw, high, low, close, spot in raw or []:
        if str(ticker) in skip:
            continue
        right = _right_lit(right_raw)
        mid = _mid_from_ohlc(close, high, low)
        exp = _as_date(expiry)
        try:
            strike_f, spot_f = float(strike), float(spot)
        except (TypeError, ValueError):
            continue
        if right is None or mid is None or exp is None:
            continue
        iv, _status = solve_iv(spot_f, strike_f, max((exp - trade_date).days, 1) / 365.0, mid, right)
        if iv is None:
            continue
        out.append(
            {
                "option_ticker": str(ticker),
                "underlying": str(und).strip().upper(),
                "iv": iv,
                "underlying_price": spot_f,
                "expiry": exp,
                "strike": strike_f,
                "option_right": right,
            }
        )
    return out


def fetch_atm_source_rows(
    conn: Any,
    trade_date: date,
    *,
    underlyings: Sequence[str] | None = None,
    solve_missing: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    """Reconstructed rows plus, when ``solve_missing``, Brent for the contracts they
    lack; the live snapshot only when neither has anything."""
    try:
        rows = fetch_reconstructed_iv_rows_for_date(conn, trade_date, underlyings=underlyings)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        rows = []
    if solve_missing:
        rows = rows + fetch_option_daily_brent_rows_for_date(
            conn,
            trade_date,
            underlyings=underlyings,
            exclude=(r.get("option_ticker") for r in rows),
        )
    if rows:
        return rows, IV_SOURCE_RECONSTRUCTED
    return (
        fetch_snapshot_iv_rows_for_date(conn, trade_date, underlyings=underlyings),
        IV_SOURCE_SNAPSHOT,
    )


def compute_atm_iv_for_date(
    conn: Any,
    *,
    trade_date: date,
    underlyings: Sequence[str] | None = None,
    solve_missing: bool = True,
) -> dict[str, Any]:
    """Compute ATM IV for all (underlying, expiry) on ``trade_date`` and upsert."""
    snap_rows, iv_source = fetch_atm_source_rows(
        conn, trade_date, underlyings=underlyings, solve_missing=solve_missing
    )
    if not snap_rows:
        return {
            "trade_date": trade_date.isoformat(),
            "groups": 0,
            "rows_written": 0,
            "symbols": 0,
            "iv_source": iv_source,
        }

    groups: dict[tuple[str, date], list[dict[str, Any]]] = {}
    for r in snap_rows:
        und = str(r.get("underlying") or "").strip().upper()
        exp = _as_date(r.get("expiry"))
        if not und or exp is None:
            continue
        groups.setdefault((und, exp), []).append(r)

    now = datetime.now(timezone.utc)
    upsert_rows: list[tuple[Any, ...]] = []
    for (symbol, expiry), rows in sorted(groups.items()):
        spot = representative_spot(rows)
        if spot is None:
            continue
        items = build_expiry_side_items(rows, spot, max_moneyness=ATM_MAX_MONEYNESS)
        atm_iv, _iv_c, _iv_p, best_strike = atm_iv_from_side_items(items)
        if atm_iv is None or best_strike is None:
            continue
        upsert_rows.append(
            (
                symbol,
                trade_date,
                expiry,
                float(best_strike),
                float(atm_iv),
                float(spot),
                iv_source,
                now,
            )
        )

    # Replace, not merge: an expiry that no longer qualifies must not keep yesterday's row.
    sourced = sorted({symbol for symbol, _expiry in groups})
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM features.option_metric_atm_iv_daily
            WHERE trade_date = %s AND symbol = ANY(%s)
            """,
            (trade_date, sourced),
        )
    if not upsert_rows:
        conn.commit()
    n = batch_upsert(
        conn,
        "features.option_metric_atm_iv_daily",
        _COLS,
        upsert_rows,
        conflict_keys=("symbol", "trade_date", "expiry"),
        update_cols=(
            "atm_strike",
            "atm_iv",
            "underlying_price",
            "iv_source",
            "computed_at",
        ),
        set_fetched_at=False,
    )
    symbols = sorted({r[0] for r in upsert_rows})
    return {
        "trade_date": trade_date.isoformat(),
        "groups": len(upsert_rows),
        "rows_written": n,
        "symbols": len(symbols),
        "iv_source": iv_source,
    }
