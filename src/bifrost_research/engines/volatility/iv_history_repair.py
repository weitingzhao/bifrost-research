"""One-shot repair of the IV history the pre-P3 snapshot model left behind (2026-09-23).

Until plugin 0.14 (P3, 2026-09-08) ``raw_market.option_snapshot.snapshot_ts`` was the
contract's last trade. A contract quoted in August that last traded in June was
projected into ``features.option_iv_reconstructed_daily`` as a June row carrying
August's IV against June's spot. P3 re-stamped the raw rows to observation time; the
projections were never redone. Before 2026-08-05 (the first observation) they are
the only rows that table has, and the ATM IV, IV30, VRP and IV percentile built on
them are the June–July readings of 0.04–2.9 (PLTR 06-25: strikes 280/310 at spot 107).

Steps, each dry-run (counts only) unless ``--apply``:

1. purge     — vendor rows dated before P3 with no raw observation that NY day
2. reproject — vendor rows from raw for [first observation, end]: pre-P3 fetches
               overwrote some in place with later values, and until 0.106.0 the
               daily Brent pass overwrote vendor rows with last-trade inversions
3. solve     — Brent over ``raw_market.option_daily`` for ``--solve`` symbols
4. derive    — session by session, oldest first: ATM IV (replacing the day),
               IV percentile, VRP; then fwd_ret_20d on the VRP rows it wrote

Usage::

    python -m bifrost_research.engines.volatility.iv_history_repair
    python -m bifrost_research.engines.volatility.iv_history_repair --solve cohort --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Sequence
from zoneinfo import ZoneInfo

from bifrost_research.db.calendar import load_symbols_from_env_or_query, union_iv_radar_benchmarks
from bifrost_research.db.conn import connect
from bifrost_research.engines.volatility.atm_iv import compute_atm_iv_for_date
from bifrost_research.engines.volatility.iv_percentile import compute_iv_percentile_for_date
from bifrost_research.engines.volatility.iv_solver import (
    DTE_MAX,
    DTE_MIN,
    STRIKE_HI,
    STRIKE_LO,
    project_vendor_snapshot_window,
    solve_symbol_window,
)
from bifrost_research.engines.vrp.compute import compute_vrp_for_date
from bifrost_research.engines.vrp.entry import backfill_fwd_ret_20d
from bifrost_research.schema.schemas import TABLE_OPTION_IV_RECONSTRUCTED_DAILY as RECON

logger = logging.getLogger(__name__)

FIRST_OBSERVATION = date(2026, 8, 5)  # min(snapshot_ts) in raw once P3 re-stamped it
P3_CUTOVER = date(2026, 9, 9)  # projections from here on read observation-time rows

_NO_SOURCE = """
    r.solver_status = 'vendor_snapshot'
    AND r.trade_date >= %s AND r.trade_date < %s
    AND NOT EXISTS (
        SELECT 1 FROM raw_market.option_snapshot os
        WHERE os.option_ticker = r.option_ticker
          AND os.snapshot_ts >= (r.trade_date::timestamp AT TIME ZONE 'America/New_York')
          AND os.snapshot_ts < ((r.trade_date + 1)::timestamp AT TIME ZONE 'America/New_York'))
"""


def windows(start: date, end: date, days: int) -> Iterator[tuple[date, date]]:
    """Inclusive [lo, hi] chunks of at most ``days`` calendar days covering [start, end]."""
    lo = start
    while lo <= end:
        hi = min(lo + timedelta(days=days - 1), end)
        yield lo, hi
        lo = hi + timedelta(days=1)


def _scalar(conn: Any, sql: str, params: Sequence[Any] = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def purge_fossils(conn: Any, *, apply: bool) -> dict[str, Any]:
    """Delete vendor rows before P3 that no raw observation backs, week by week.

    Only valid while raw still holds its first observation day: once retention trims
    past it, "no raw row" stops meaning "never observed", so this refuses.
    """
    raw_first = _scalar(
        conn,
        "SELECT MIN(DATE(timezone('America/New_York', snapshot_ts))) FROM raw_market.option_snapshot",
    )
    if raw_first is None or raw_first > FIRST_OBSERVATION:
        raise RuntimeError(f"raw_market.option_snapshot starts {raw_first}, after {FIRST_OBSERVATION}: refusing")
    oldest = _scalar(conn, f"SELECT MIN(trade_date) FROM {RECON} WHERE solver_status = 'vendor_snapshot'")
    if oldest is None:
        return {"step": "purge", "rows": 0, "chunks": []}
    chunks = []
    total = 0
    for lo, hi in windows(oldest, P3_CUTOVER - timedelta(days=1), 7):
        params = (lo, hi + timedelta(days=1))
        n = int(_scalar(conn, f"SELECT COUNT(*) FROM {RECON} r WHERE {_NO_SOURCE}", params) or 0)
        if n and apply:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {RECON} r WHERE {_NO_SOURCE}", params)
                deleted = cur.rowcount
            if deleted != n:
                conn.rollback()
                raise RuntimeError(f"purge {lo}..{hi}: counted {n}, delete touched {deleted}; rolled back")
            conn.commit()
        if n:
            chunks.append({"from": lo.isoformat(), "to": hi.isoformat(), "rows": n})
        total += n
    after = int(
        _scalar(conn, f"SELECT COUNT(*) FROM {RECON} r WHERE {_NO_SOURCE}", (P3_CUTOVER, date.today() + timedelta(days=1)))
        or 0
    )
    return {"step": "purge", "rows": total, "applied": apply, "chunks": chunks, "unbacked_after_p3": after}


def reproject_vendor(conn: Any, symbols: Sequence[str], end: date, *, apply: bool) -> dict[str, Any]:
    """Re-read vendor IV from raw for every observation-time day up to ``end``."""
    if not apply:
        n = _scalar(
            conn,
            """
            SELECT COUNT(*) FROM raw_market.option_snapshot
            WHERE underlying = ANY(%s) AND iv > 0
              AND snapshot_ts >= (%s::timestamp AT TIME ZONE 'America/New_York')
              AND snapshot_ts < (%s::timestamp AT TIME ZONE 'America/New_York')
            """,
            (list(symbols), FIRST_OBSERVATION, end + timedelta(days=1)),
        )
        return {"step": "reproject", "raw_rows": int(n or 0), "symbols": len(symbols), "applied": False}
    written = 0
    for sym in symbols:
        for lo, hi in windows(FIRST_OBSERVATION, end, 14):
            written += int(project_vendor_snapshot_window(conn, sym, lo, hi).get("rows_written") or 0)
    return {"step": "reproject", "rows_written": written, "symbols": len(symbols), "applied": True}


def solve_scope(conn: Any, scope: str, universe: Sequence[str]) -> list[str]:
    """``none`` · ``cohort`` (symbols that had IV30 before the first observation) ·
    ``universe`` · or a comma-separated list."""
    if scope == "none":
        return []
    if scope == "universe":
        return list(universe)
    if scope == "cohort":
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT symbol FROM features.stock_signal_vrp_daily
                WHERE trade_date < %s AND atm_iv_30d IS NOT NULL ORDER BY 1
                """,
                (FIRST_OBSERVATION,),
            )
            return [str(r[0]) for r in cur.fetchall()]
    return sorted({s.strip().upper() for s in scope.split(",") if s.strip()})


def solve_history(conn: Any, symbols: Sequence[str], start: date, end: date, *, apply: bool) -> dict[str, Any]:
    """Brent over option_daily month by month; dry run counts the contracts it would solve."""
    if not symbols:
        return {"step": "solve", "symbols": 0}
    if not apply:
        n = 0
        for lo, hi in windows(start, end, 31):
            n += int(
                _scalar(
                    conn,
                    """
                    SELECT COUNT(*) FROM raw_market.option_daily o
                    JOIN raw_market.stock_daily s ON s.symbol = o.underlying AND s.bar_date = o.bar_date
                    WHERE o.underlying = ANY(%s) AND o.bar_date BETWEEN %s AND %s
                      AND (o.expiry - o.bar_date) BETWEEN %s AND %s
                      AND o.strike BETWEEN %s * s.close AND %s * s.close
                    """,
                    (list(symbols), lo, hi, DTE_MIN, DTE_MAX, STRIKE_LO, STRIKE_HI),
                )
                or 0
            )
        return {"step": "solve", "symbols": len(symbols), "contract_days": n, "applied": False}
    by_status: dict[str, int] = {}
    kept = 0
    for sym in symbols:
        for lo, hi in windows(start, end, 31):
            out = solve_symbol_window(conn, sym, lo, hi)
            kept += int(out.get("vendor_kept") or 0)
            for k, v in (out.get("by_status") or {}).items():
                by_status[k] = by_status.get(k, 0) + int(v)
    return {"step": "solve", "symbols": len(symbols), "by_status": by_status, "vendor_kept": kept, "applied": True}


def sessions(conn: Any, start: date, end: date) -> list[date]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT bar_date FROM raw_market.stock_daily WHERE symbol = 'SPY' AND bar_date BETWEEN %s AND %s ORDER BY 1",
            (start, end),
        )
        return [r[0] for r in cur.fetchall()]


def derive(
    conn: Any,
    days: Sequence[date],
    universe: Sequence[str],
    *,
    apply: bool,
    progress: Callable[[date], None] | None = None,
) -> dict[str, Any]:
    """Rebuild ATM IV → IV percentile → VRP for each session, oldest first, so every
    percentile ranks against history already rebuilt."""
    if not apply:
        return {"step": "derive", "sessions": len(days), "applied": False}
    atm = pct = vrp = 0
    for td in days:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM features.option_metric_atm_iv_daily WHERE trade_date = %s", (td,))
            cur.execute("DELETE FROM features.option_metric_iv_percentile_daily WHERE trade_date = %s", (td,))
        conn.commit()
        atm += int(compute_atm_iv_for_date(conn, trade_date=td, underlyings=universe).get("rows_written") or 0)
        pct += int(compute_iv_percentile_for_date(conn, trade_date=td, underlyings=universe).get("rows_written") or 0)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol FROM features.option_metric_atm_iv_daily WHERE trade_date = %s
                UNION SELECT symbol FROM features.stock_signal_vrp_daily WHERE trade_date = %s
                """,
                (td, td),
            )
            vrp_syms = sorted({str(r[0]) for r in cur.fetchall()})
        if vrp_syms:
            vrp += int(compute_vrp_for_date(conn, trade_date=td, underlyings=vrp_syms).get("rows_written") or 0)
        if progress:
            progress(td)
    return {"step": "derive", "sessions": len(days), "atm_rows": atm, "pct_rows": pct, "vrp_rows": vrp, "applied": True}


def run(*, solve: str, start: date | None, end: date, apply: bool) -> dict[str, Any]:
    conn = connect()
    try:
        universe = union_iv_radar_benchmarks(load_symbols_from_env_or_query(conn))
        solve_syms = solve_scope(conn, solve, universe)
        if start is None:
            atm_first = _scalar(conn, "SELECT MIN(trade_date) FROM features.option_metric_atm_iv_daily")
            od_first = _scalar(conn, "SELECT MIN(bar_date) FROM raw_market.option_daily") if solve_syms else None
            start = min(d for d in (atm_first, od_first, FIRST_OBSERVATION) if d is not None)
        steps = [
            purge_fossils(conn, apply=apply),
            reproject_vendor(conn, universe, end, apply=apply),
            solve_history(conn, solve_syms, start, end, apply=apply),
        ]
        days = sessions(conn, start, end)
        steps.append(derive(conn, days, universe, apply=apply, progress=lambda d: logger.info("derived %s", d)))
        if apply:
            steps.append({"step": "fwd_ret_20d", **backfill_fwd_ret_20d(lookback_days=(end - start).days + 1, as_of=end)})
        return {"start": start.isoformat(), "end": end.isoformat(), "solve": solve, "universe": len(universe), "steps": steps}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair IV history left by the pre-P3 snapshot model")
    parser.add_argument("--solve", default="none", help="none | cohort | universe | SYM,SYM")
    parser.add_argument("--start", default=None, help="YYYY-MM-DD (default: oldest ATM row / option_daily bar)")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD (default: today in New York)")
    parser.add_argument("--apply", action="store_true", help="write; without it every step only counts")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run(
        solve=args.solve,
        start=date.fromisoformat(args.start) if args.start else None,
        end=date.fromisoformat(args.end) if args.end else datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date(),
        apply=args.apply,
    )
    print(json.dumps(result, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
