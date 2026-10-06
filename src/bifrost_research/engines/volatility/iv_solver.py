"""Historical IV solver — Brent BS inversion + dual-source projection (IDS).

Sources:
  1. ``raw_market.option_daily`` OHLCV → Brent invert → ``solver_status=ok|…``
  2. ``raw_market.option_snapshot`` vendor IV → ``solver_status=vendor_snapshot``

Writes ``features.option_iv_reconstructed_daily``. See ``docs/IV_SOLVER_SPEC.md``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Literal, Sequence

from bifrost_research.db.calendar import ny_today
from bifrost_research.db.fastcount import breakdown_with_dominant, distinct_count, estimate_rows
from bifrost_research.db.upsert import batch_upsert
from bifrost_research.engines.adjusted_contracts import not_adjusted_contract_sql
from bifrost_research.pricing import (
    IV_HI,
    IV_LO,
    bs_delta,
    bs_gamma,
    bs_price,
    load_rate_curve,
    solve_iv,
)
from bifrost_research.schema.schemas import TABLE_OPTION_IV_RECONSTRUCTED_DAILY

logger = logging.getLogger(__name__)

_COLS = (
    "symbol",
    "option_ticker",
    "trade_date",
    "strike",
    "expiry",
    "option_right",
    "mid_price",
    "spot",
    "tte_years",
    "iv",
    "delta",
    "gamma",
    "solver_status",
    "computed_at",
)

STRIKE_LO = 0.80
STRIKE_HI = 1.20
DTE_MIN = 5
DTE_MAX = 90

# A snapshot row stands for the session it is stamped with only if it was fetched
# then (Friday sessions are re-fetched over the weekend: lag 0–2 days on DEV). Before
# plugin P3 (2026-09-08) snapshot_ts was the last trade, so an August fetch of a
# contract that last traded in June was stamped June; those rows were fetched weeks
# after their stamp. Research checks the invariant instead of trusting it.
SNAPSHOT_MAX_FETCH_LAG_DAYS = 3


def as_traded_close(alias: str) -> str:
    """SQL expression: ``alias`` (a ``stock_daily`` row) at the price it traded at.

    The Plugin fetches bars ``adjusted=true``, adjusted for every split gone ex by
    the fetch, while an option contract keeps the strike it was listed with. So a
    bar dated before a split and fetched after it is on a different scale from
    that day's chain: BKNG 2026-01-15 closed 207.72 in ``stock_daily`` against
    strikes 3,600-6,660 (25-for-1, ex 2026-04-06), and no contract sat within the
    ATM band on any of the 130 sessions before the ex-date. Multiplying back the
    splits between the bar and its fetch restores the as-traded close; a bar
    fetched before its split already is one, which is why the fetch bounds it.

    Spin-offs cannot be undone that way: the vendor folds them into the adjusted
    series and reports none (HON 2025-10-29 is 200.65 adjusted, 212.89 as traded,
    212.10 by its own chain's parity). From plugin 0.74.0 the session's printed
    close is stored beside the adjusted one as ``close_unadjusted``; it leads, and
    the split arithmetic only covers a row the plugin has not filled.
    """
    return (
        f"COALESCE({alias}.close_unadjusted, "
        f"{alias}.close * coalesce(("
        "SELECT exp(sum(ln(ca.ratio_to / ca.ratio_from)))"
        " FROM raw_market.corporate_action ca"
        f" WHERE ca.symbol = {alias}.symbol AND ca.action_type = 'split'"
        " AND ca.ratio_from > 0 AND ca.ratio_to > 0 AND ca.ratio_from <> ca.ratio_to"
        f" AND ca.ex_date > {alias}.bar_date"
        f" AND ca.ex_date <= DATE(timezone('America/New_York', {alias}.fetched_at))"
        "), 1))"
    )


def observed_near_session(alias: str) -> str:
    """SQL predicate: ``alias`` (a snapshot row) was fetched within the lag of its session."""
    return (
        f"DATE(timezone('America/New_York', {alias}.fetched_at))"
        f" - DATE(timezone('America/New_York', {alias}.snapshot_ts)) <= {SNAPSHOT_MAX_FETCH_LAG_DAYS}"
    )


def _mid_from_ohlc(close: Any, high: Any, low: Any) -> float | None:
    try:
        if close is not None and float(close) > 0:
            return float(close)
    except (TypeError, ValueError):
        pass
    try:
        if high is not None and low is not None:
            h, lo = float(high), float(low)
            if h > 0 and lo > 0:
                return 0.5 * (h + lo)
    except (TypeError, ValueError):
        pass
    return None


def _passes_filters(spot: float, strike: float, dte: int) -> bool:
    if dte < DTE_MIN or dte > DTE_MAX:
        return False
    if spot <= 0 or strike <= 0:
        return False
    return STRIKE_LO * spot <= strike <= STRIKE_HI * spot


def _right_lit(raw: Any) -> Literal["C", "P"] | None:
    s = str(raw or "").strip().upper()
    if s in ("C", "CALL"):
        return "C"
    if s in ("P", "PUT"):
        return "P"
    return None


def upsert_reconstructed(conn: Any, rows: Sequence[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    return batch_upsert(
        conn,
        TABLE_OPTION_IV_RECONSTRUCTED_DAILY,
        _COLS,
        rows,
        conflict_keys=("symbol", "option_ticker", "trade_date"),
        update_cols=(
            "strike",
            "expiry",
            "option_right",
            "mid_price",
            "spot",
            "tte_years",
            "iv",
            "delta",
            "gamma",
            "solver_status",
            "computed_at",
        ),
        set_fetched_at=False,
    )


def _vendor_keys(conn: Any, symbol: str, start_date: date, end_date: date) -> set[tuple[str, date]]:
    """(option_ticker, trade_date) already carrying vendor IV in [start, end]."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT option_ticker, trade_date
            FROM {TABLE_OPTION_IV_RECONSTRUCTED_DAILY}
            WHERE symbol = %s
              AND trade_date BETWEEN %s AND %s
              AND solver_status = 'vendor_snapshot'
            """,
            (symbol, start_date, end_date),
        )
        return {(str(r[0]), r[1]) for r in (cur.fetchall() or [])}


def solve_symbol_window(
    conn: Any,
    symbol: str,
    start_date: date,
    end_date: date,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Brent-invert option_daily OHLCV for one symbol over [start, end].

    A contract-day that already has vendor IV keeps it: the vendor reading is taken
    at the close, while Brent inverts the day's last trade against the closing spot.
    """
    sym = symbol.strip().upper()
    vendor = _vendor_keys(conn, sym, start_date, end_date)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH sp AS (
              SELECT s.symbol, s.bar_date, {as_traded_close("s")} AS spot
              FROM raw_market.stock_daily s
              WHERE s.symbol = %s
                AND s.bar_date BETWEEN %s AND %s
                AND s.close IS NOT NULL AND s.close > 0
            )
            SELECT o.option_ticker, o.underlying, o.bar_date, o.expiry, o.strike,
                   o.option_right, o.open, o.high, o.low, o.close, sp.spot
            FROM raw_market.option_daily o
            JOIN sp ON sp.symbol = o.underlying AND sp.bar_date = o.bar_date
            WHERE o.underlying = %s
              AND o.bar_date BETWEEN %s AND %s
              AND {not_adjusted_contract_sql("o.option_ticker")}
            ORDER BY o.bar_date, o.option_ticker
            """,
            (sym, start_date, end_date, sym, start_date, end_date),
        )
        raw = cur.fetchall() or []

    now = datetime.now(timezone.utc)
    # The Treasury rate of each session, the same reader the backtester uses (TD-110).
    rates = load_rate_curve(conn, start_date, end_date)
    out_rows: list[tuple[Any, ...]] = []
    status_counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []

    vendor_kept = 0
    for r in raw:
        ticker, und, bar_d, expiry, strike, right_raw = r[0], r[1], r[2], r[3], r[4], r[5]
        high, low, close, spot = r[7], r[8], r[9], r[10]
        if (str(ticker), bar_d) in vendor:
            vendor_kept += 1
            continue
        right = _right_lit(right_raw)
        mid = _mid_from_ohlc(close, high, low)
        try:
            strike_f = float(strike)
            spot_f = float(spot)
        except (TypeError, ValueError):
            continue
        if right is None or mid is None or expiry is None or bar_d is None:
            continue
        dte = (expiry - bar_d).days
        if not _passes_filters(spot_f, strike_f, dte):
            continue
        tte = max(dte, 1) / 365.0
        rate = rates.on_or_before(bar_d)
        iv, status = solve_iv(spot_f, strike_f, tte, mid, right, rate=rate)
        status_counts[status] = status_counts.get(status, 0) + 1
        delta = gamma = None
        if iv is not None:
            delta = bs_delta(spot_f, strike_f, tte, iv, right=right, rate=rate)
            gamma = bs_gamma(spot_f, strike_f, tte, iv, rate=rate)
        row = (
            str(und).strip().upper(),
            str(ticker),
            bar_d,
            strike_f,
            expiry,
            right,
            mid,
            spot_f,
            tte,
            iv,
            delta,
            gamma,
            status,
            now,
        )
        out_rows.append(row)
        if len(samples) < 3:
            samples.append(
                {
                    "option_ticker": ticker,
                    "trade_date": bar_d.isoformat() if hasattr(bar_d, "isoformat") else str(bar_d),
                    "strike": strike_f,
                    "mid": mid,
                    "spot": spot_f,
                    "iv": iv,
                    "solver_status": status,
                }
            )

    if dry_run:
        return {
            "symbol": sym,
            "source": "option_daily",
            "dry_run": True,
            "rows": len(out_rows),
            "by_status": status_counts,
            "vendor_kept": vendor_kept,
            "sample": samples,
        }
    n = upsert_reconstructed(conn, out_rows)
    return {
        "symbol": sym,
        "source": "option_daily",
        "rows_written": n,
        "by_status": status_counts,
        "vendor_kept": vendor_kept,
    }


# A session's snapshot is partial when it carries fewer than DEGRADED_COUNT_RATIO of the
# IV-bearing contracts of the symbol's previous sessions AND a median IV above
# DEGRADED_IV_RATIO times theirs. 2026-09-22 on DEV: 12 symbols' EOD snapshots held about
# half their contracts at 2–3× the IV (CDW 27 contracts at 1.21 against 51 at 0.46), with
# the day before and after normal. Either signal alone is a thin chain or a real move;
# together over Aug 5 – Sep 24 they flagged 13 of 6,340 symbol-days, all of that kind.
DEGRADED_COUNT_RATIO = 0.6
DEGRADED_IV_RATIO = 1.8
DEGRADED_PRIOR_SESSIONS = 5


def degraded_sessions(conn: Any, symbol: str, start_date: date, end_date: date) -> set[date]:
    """Sessions in [start, end] whose snapshot for ``symbol`` looks partial (see above),
    judged against up to ``DEGRADED_PRIOR_SESSIONS`` earlier sessions (at least one)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DATE(timezone('America/New_York', os.snapshot_ts)) AS d,
                   COUNT(DISTINCT os.option_ticker),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY os.iv)
            FROM raw_market.option_snapshot os
            WHERE os.underlying = %s AND os.iv > 0
              AND os.snapshot_ts >= (%s::timestamp AT TIME ZONE 'America/New_York')
              AND os.snapshot_ts < (%s::timestamp AT TIME ZONE 'America/New_York')
              AND {observed_near_session("os")}
              AND {not_adjusted_contract_sql("os.option_ticker")}
            GROUP BY 1
            ORDER BY 1
            """,
            (symbol, start_date - timedelta(days=21), end_date + timedelta(days=1)),
        )
        stats = [(r[0], int(r[1]), float(r[2])) for r in (cur.fetchall() or []) if r[2] is not None]
    out: set[date] = set()
    for i, (d, n, iv) in enumerate(stats):
        if d < start_date:
            continue
        prior = stats[max(0, i - DEGRADED_PRIOR_SESSIONS):i]
        if not prior:
            continue
        n_med = median(p[1] for p in prior)
        iv_med = median(p[2] for p in prior)
        if n < DEGRADED_COUNT_RATIO * n_med and iv > DEGRADED_IV_RATIO * iv_med:
            out.add(d)
    return out


def project_vendor_snapshot_window(
    conn: Any,
    symbol: str,
    start_date: date,
    end_date: date,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Project Polygon snapshot IV into reconstructed table (depth path).

    Sessions ``degraded_sessions`` flags are not projected, and vendor rows already
    stored for them are deleted, so ATM IV falls back to Brent from option_daily there.
    """
    sym = symbol.strip().upper()
    degraded = degraded_sessions(conn, sym, start_date, end_date)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (v.option_ticker, DATE(timezone('America/New_York', v.snapshot_ts)))
              v.option_ticker,
              UPPER(TRIM(v.underlying)),
              DATE(timezone('America/New_York', v.snapshot_ts)) AS trade_date,
              oc.expiry,
              oc.strike,
              oc.option_right,
              v.iv,
              v.underlying_price AS spot,
              v.delta,
              v.gamma
            FROM raw_market.v_option_snapshot_with_stock v
            INNER JOIN raw_market.option_contract oc
              ON oc.option_ticker = v.option_ticker
            WHERE v.underlying = %s
              AND DATE(timezone('America/New_York', v.snapshot_ts)) BETWEEN %s AND %s
              AND v.iv IS NOT NULL AND v.iv > 0
              AND v.underlying_price IS NOT NULL AND v.underlying_price > 0
              AND {observed_near_session("v")}
              AND {not_adjusted_contract_sql("v.option_ticker")}
            ORDER BY v.option_ticker,
                     DATE(timezone('America/New_York', v.snapshot_ts)),
                     v.snapshot_ts DESC
            """,
            (sym, start_date, end_date),
        )
        raw = cur.fetchall() or []

    now = datetime.now(timezone.utc)
    rates = load_rate_curve(conn, start_date, end_date)
    out_rows: list[tuple[Any, ...]] = []
    for r in raw:
        ticker, und, trade_d, expiry, strike, right_raw = r[0], r[1], r[2], r[3], r[4], r[5]
        iv, spot, delta, gamma = r[6], r[7], r[8], r[9]
        right = _right_lit(right_raw)
        try:
            strike_f = float(strike)
            spot_f = float(spot)
            iv_f = float(iv)
        except (TypeError, ValueError):
            continue
        if right is None or expiry is None or trade_d is None or trade_d in degraded:
            continue
        dte = (expiry - trade_d).days
        # Vendor path: keep all positive-DTE contracts with sane IV (depth).
        # Moneyness/DTE filters apply only to OHLCV Brent path.
        if dte < 1:
            continue
        # Normalize percent-style IV if vendor stored > 3
        if iv_f > 3.0:
            iv_f = iv_f / 100.0
        if not (IV_LO <= iv_f <= IV_HI):
            continue
        tte = max(dte, 1) / 365.0
        rate = rates.on_or_before(trade_d)
        # Approximate mid from BS for audit trail
        mid = bs_price(spot_f, strike_f, tte, iv_f, right=right, rate=rate)
        out_rows.append(
            (
                und,
                str(ticker),
                trade_d,
                strike_f,
                expiry,
                right,
                mid,
                spot_f,
                tte,
                iv_f,
                float(delta) if delta is not None else bs_delta(spot_f, strike_f, tte, iv_f, right=right, rate=rate),
                float(gamma) if gamma is not None else bs_gamma(spot_f, strike_f, tte, iv_f, rate=rate),
                "vendor_snapshot",
                now,
            )
        )

    if dry_run:
        return {
            "symbol": sym,
            "source": "option_snapshot",
            "dry_run": True,
            "rows": len(out_rows),
            "degraded_sessions": sorted(d.isoformat() for d in degraded),
            "sample": [
                {
                    "option_ticker": r[1],
                    "trade_date": r[2].isoformat() if hasattr(r[2], "isoformat") else str(r[2]),
                    "iv": r[9],
                    "solver_status": r[12],
                }
                for r in out_rows[:3]
            ],
        }
    dropped = 0
    if degraded:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM {TABLE_OPTION_IV_RECONSTRUCTED_DAILY}
                WHERE symbol = %s AND trade_date = ANY(%s) AND solver_status = 'vendor_snapshot'
                """,
                (sym, sorted(degraded)),
            )
            dropped = int(cur.rowcount or 0)
        if not out_rows:
            conn.commit()
    n = upsert_reconstructed(conn, out_rows)
    return {
        "symbol": sym,
        "source": "option_snapshot",
        "rows_written": n,
        "degraded_sessions": sorted(d.isoformat() for d in degraded),
        "degraded_rows_dropped": dropped,
    }


def run_cohort(
    conn: Any,
    *,
    symbols: Sequence[str],
    lookback_days: int = 252,
    as_of: date | None = None,
    source: Literal["all", "daily", "snapshot"] = "all",
    dry_run: bool = False,
) -> dict[str, Any]:
    end = as_of or ny_today()
    start = end.fromordinal(end.toordinal() - int(lookback_days * 1.5))  # calendar buffer
    # Prefer trading-day lookback via stock calendar when available
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bar_date FROM raw_market.stock_daily
                WHERE symbol = 'SPY' AND bar_date <= %s
                ORDER BY bar_date DESC
                LIMIT %s
                """,
                (end, lookback_days),
            )
            days = [r[0] for r in (cur.fetchall() or [])]
            if days:
                start = min(days)
    except Exception:
        conn.rollback()

    per: list[dict[str, Any]] = []
    total = 0
    for sym in symbols:
        if source in ("all", "snapshot"):
            one = project_vendor_snapshot_window(
                conn, sym, start, end, dry_run=dry_run
            )
            per.append(one)
            total += int(one.get("rows_written") or one.get("rows") or 0)
        if source in ("all", "daily"):
            one = solve_symbol_window(conn, sym, start, end, dry_run=dry_run)
            per.append(one)
            total += int(one.get("rows_written") or one.get("rows") or 0)

    coverage = None
    if not dry_run:
        coverage = coverage_report(conn)
    return {
        "mode": "cohort",
        "lookback_days": lookback_days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "source": source,
        "symbols": len(symbols),
        "rows_written": total,
        "per_symbol": per,
        "coverage": coverage,
        "dry_run": dry_run,
    }


#: The solver_status nearly every row carries; the rest are counted off a
#: partial index (0.116.0).
DOMINANT_STATUS = "vendor_snapshot"


def _coverage_fast(conn: Any) -> dict[str, Any]:
    """The same report without a full scan (0.116.0) — rows are the planner's estimate."""
    table = TABLE_OPTION_IV_RECONSTRUCTED_DAILY
    with conn.cursor() as cur:
        total, estimated = estimate_rows(cur, table)
        by_status = breakdown_with_dominant(cur, table, "solver_status", DOMINANT_STATUS, total)
        symbols = distinct_count(cur, table, "symbol")
        dates = distinct_count(cur, table, "trade_date")
        # Rows without an IV are the few; counted off the partial index.
        cur.execute(f"SELECT COUNT(*)::bigint FROM {table} WHERE iv IS NULL")
        without_iv = int((cur.fetchone() or (0,))[0] or 0)
    ok = int(by_status.get("ok") or 0) + int(by_status.get("vendor_snapshot") or 0)
    return {
        "rows": total,
        "rows_estimated": estimated,
        "symbols": symbols,
        "distinct_dates": dates,
        "with_iv": max(total - without_iv, 0),
        "by_status": by_status,
        "solver_ok_pct": (ok / total) if total else None,
    }


def coverage_report(conn: Any, *, fast: bool = False) -> dict[str, Any]:
    """Coverage of the reconstructed-IV table.

    ``fast`` reads it off indexes and the planner's row estimate — what a
    reader-facing endpoint under the 2s statement timeout needs. Without it
    the counts are exact full scans, which a cohort run's own summary wants
    right after it writes.
    """
    if fast:
        return _coverage_fast(conn)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)::bigint,
                   COUNT(DISTINCT symbol)::bigint,
                   COUNT(DISTINCT trade_date)::bigint
            FROM {TABLE_OPTION_IV_RECONSTRUCTED_DAILY}
            """
        )
        row = cur.fetchone() or (0, 0, 0)
        cur.execute(
            f"""
            SELECT solver_status, COUNT(*)::bigint
            FROM {TABLE_OPTION_IV_RECONSTRUCTED_DAILY}
            GROUP BY 1
            """
        )
        by_status = {str(r[0]): int(r[1]) for r in cur.fetchall()}
        cur.execute(
            f"""
            SELECT COUNT(*)::bigint
            FROM {TABLE_OPTION_IV_RECONSTRUCTED_DAILY}
            WHERE iv IS NOT NULL
            """
        )
        with_iv = int((cur.fetchone() or (0,))[0] or 0)
    total = int(row[0] or 0)
    ok = int(by_status.get("ok") or 0) + int(by_status.get("vendor_snapshot") or 0)
    return {
        "rows": total,
        "symbols": int(row[1] or 0),
        "distinct_dates": int(row[2] or 0),
        "with_iv": with_iv,
        "by_status": by_status,
        "solver_ok_pct": (ok / total) if total else None,
    }


__all__ = [
    "solve_iv",
    "bs_gamma",
    "solve_symbol_window",
    "project_vendor_snapshot_window",
    "run_cohort",
    "coverage_report",
    "upsert_reconstructed",
]
