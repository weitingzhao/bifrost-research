"""IV Percentile / Rank daily: features.option_metric_atm_iv_daily → iv_percentile_daily.

Current IV for a symbol+trade_date = IV30 (``atm_iv.iv30_from_expiries``), the same
reading VRP stores as ``atm_iv_30d``. Until 0.108.0 it was the median across every
expiry, which moved with chain depth: LEAPS-heavy chains pulled it up, a day with
only weeklies pulled it toward pin noise.

IV Percentile: fraction of historical daily IVs (inclusive lookback window) <= current × 100.
IV Rank: (current − min) / (max − min) × 100; when max == min → 50.0.
Both stay NULL until the window holds ``MIN_LOOKBACK_DAYS`` sessions (capped at the
window): the ``_1y`` fields read 75 and 11 sessions for PLTR and AMD on 2026-09-23.

History is this table's own ``iv_current`` for the prior sessions (as VRP ranks its
stored ``vrp_60d``), so each day reads one day of ATM rows, not a year of them for
the whole universe; recompute a range oldest first.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Sequence

from bifrost_research.db.upsert import batch_upsert
from bifrost_research.engines.volatility.atm_iv import IV30_MAX_DTE, IV30_MIN_DTE, iv30_from_expiries

_COLS = (
    "symbol",
    "trade_date",
    "iv_current",
    "iv_percentile_1y",
    "iv_rank_1y",
    "lookback_days",
    "computed_at",
)

DEFAULT_PERCENTILE_WINDOW = 252
MIN_LOOKBACK_DAYS = 126


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


def iv_percentile(current: float, history: Sequence[float]) -> float | None:
    """Fraction of history values <= current, as 0–100. Empty history → None."""
    if not history:
        return None
    n = len(history)
    le = sum(1 for v in history if float(v) <= current)
    return round(100.0 * le / n, 4)


def iv_rank(current: float, history: Sequence[float]) -> float | None:
    """(current-min)/(max-min)*100; max==min → 50. Empty history → None."""
    if not history:
        return None
    lo = min(float(v) for v in history)
    hi = max(float(v) for v in history)
    if hi == lo:
        return 50.0
    return round(100.0 * (current - lo) / (hi - lo), 4)


def fetch_atm_iv_rows(
    conn: Any,
    *,
    from_date: date,
    to_date: date,
    underlyings: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Load atm_iv_daily rows in [from_date, to_date] that IV30 can use (7–90 DTE)."""
    cols = ("symbol", "trade_date", "expiry", "atm_iv")
    syms = [str(s).strip().upper() for s in (underlyings or []) if str(s).strip()]
    with conn.cursor() as cur:
        if syms:
            cur.execute(
                f"""
                SELECT symbol, trade_date, expiry, atm_iv
                FROM features.option_metric_atm_iv_daily
                WHERE trade_date >= %s AND trade_date <= %s
                  AND symbol = ANY(%s)
                  AND (expiry - trade_date) BETWEEN {IV30_MIN_DTE} AND {IV30_MAX_DTE}
                ORDER BY symbol, trade_date, expiry
                """,
                (from_date, to_date, syms),
            )
        else:
            cur.execute(
                f"""
                SELECT symbol, trade_date, expiry, atm_iv
                FROM features.option_metric_atm_iv_daily
                WHERE trade_date >= %s AND trade_date <= %s
                  AND (expiry - trade_date) BETWEEN {IV30_MIN_DTE} AND {IV30_MAX_DTE}
                ORDER BY symbol, trade_date, expiry
                """,
                (from_date, to_date),
            )
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []
    return [_row_to_dict(r, cols) for r in (raw or [])]


def rollup_daily_iv_by_symbol(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[date, float]]:
    """symbol → {trade_date → IV30 from that day's expiries}."""
    buckets: dict[str, dict[date, list[tuple[Any, Any]]]] = {}
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        td = _as_date(r.get("trade_date"))
        if not sym or td is None:
            continue
        buckets.setdefault(sym, {}).setdefault(td, []).append((r.get("expiry"), r.get("atm_iv")))

    out: dict[str, dict[date, float]] = {}
    for sym, by_day in buckets.items():
        day_map: dict[date, float] = {}
        for td, pairs in by_day.items():
            rep = iv30_from_expiries(td, pairs)
            if rep is not None:
                day_map[td] = rep
        if day_map:
            out[sym] = day_map
    return out


def fetch_prior_iv_current(
    conn: Any,
    symbols: Sequence[str],
    *,
    from_date: date,
    before: date,
) -> dict[str, list[float]]:
    """symbol → stored ``iv_current`` for sessions in [from_date, before), oldest first."""
    if not symbols:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, trade_date, iv_current
            FROM features.option_metric_iv_percentile_daily
            WHERE trade_date >= %s AND trade_date < %s
              AND iv_current IS NOT NULL
              AND symbol = ANY(%s)
            ORDER BY symbol, trade_date
            """,
            (from_date, before, list(symbols)),
        )
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []
    out: dict[str, list[float]] = {}
    for r in raw or []:
        d = _row_to_dict(r, ("symbol", "trade_date", "iv_current"))
        try:
            out.setdefault(str(d["symbol"]).upper(), []).append(float(d["iv_current"]))
        except (TypeError, ValueError):
            continue
    return out


def compute_iv_percentile_for_date(
    conn: Any,
    *,
    trade_date: date,
    underlyings: Sequence[str] | None = None,
    percentile_window: int = DEFAULT_PERCENTILE_WINDOW,
) -> dict[str, Any]:
    """Compute IV percentile/rank for symbols with ATM IV on ``trade_date`` and upsert.

    History window: calendar days covering ~``percentile_window`` trading days
    (``trade_date - percentile_window * 2 - 30`` covers weekends/holidays), then at
    most ``percentile_window - 1`` prior stored IVs + the current day.
    """
    window = max(1, int(percentile_window))
    need = min(MIN_LOOKBACK_DAYS, window)
    from_date = trade_date - timedelta(days=window * 2 + 30)
    rows = fetch_atm_iv_rows(
        conn,
        from_date=trade_date,
        to_date=trade_date,
        underlyings=underlyings,
    )
    by_sym = rollup_daily_iv_by_symbol(rows)
    if not by_sym:
        return {
            "trade_date": trade_date.isoformat(),
            "groups": 0,
            "rows_written": 0,
            "symbols": 0,
            "percentile_window": window,
        }

    prior = fetch_prior_iv_current(conn, sorted(by_sym), from_date=from_date, before=trade_date)
    now = datetime.now(timezone.utc)
    upsert_rows: list[tuple[Any, ...]] = []
    for symbol, day_map in sorted(by_sym.items()):
        current = day_map.get(trade_date)
        if current is None:
            continue
        hist_vals = prior.get(symbol, [])[-(window - 1):] if window > 1 else []
        hist_vals = [*hist_vals, current]
        lookback_used = len(hist_vals)
        pct = rank = None
        if lookback_used >= need:
            pct = iv_percentile(current, hist_vals)
            rank = iv_rank(current, hist_vals)
        upsert_rows.append(
            (
                symbol,
                trade_date,
                float(current),
                pct,
                rank,
                int(lookback_used),
                now,
            )
        )

    n = batch_upsert(
        conn,
        "features.option_metric_iv_percentile_daily",
        _COLS,
        upsert_rows,
        conflict_keys=("symbol", "trade_date"),
        update_cols=(
            "iv_current",
            "iv_percentile_1y",
            "iv_rank_1y",
            "lookback_days",
            "computed_at",
        ),
        set_fetched_at=False,
    )
    return {
        "trade_date": trade_date.isoformat(),
        "groups": len(upsert_rows),
        "rows_written": n,
        "symbols": len(upsert_rows),
        "percentile_window": window,
    }
