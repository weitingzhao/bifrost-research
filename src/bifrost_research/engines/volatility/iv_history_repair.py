"""Repair of the IV history the pre-P3 snapshot model left behind (2026-09-23).

Until plugin 0.14 (P3, 2026-09-08) ``raw_market.option_snapshot.snapshot_ts`` was the
contract's last trade. A contract quoted in August that last traded in June was
projected into ``features.option_iv_reconstructed_daily`` as a June row carrying
August's IV against June's spot. P3 re-stamped the raw rows to observation time; the
projections were never redone. Before 2026-08-05 (the first observation) they are
the only rows that table has, and the ATM IV, IV30, VRP and IV percentile built on
them are the June–July readings of 0.04–2.9 (PLTR 06-25: strikes 280/310 at spot 107).

Steps, each dry-run (counts only) unless ``--apply``:

1. reproject — every vendor row raw still observes, re-read from raw: pre-P3 fetches
               overwrote some in place with later values, and until 0.106.0 the
               daily Brent pass overwrote vendor rows with last-trade inversions
2. purge     — vendor rows dated before P3 that no projection has confirmed since P3
               (``computed_at`` before the cutover). Step 1 re-stamps every row raw
               backs, so what is left was never observed on its session; the test
               reads only this table and stays true after raw retention trims the
               days it was run on. Later runs find nothing to delete.
3. derive    — session by session, oldest first: ATM IV (replacing the day; Brent
               from ``option_daily`` for what the table lacks, so every symbol in the
               universe back to the first option bar), IV percentile; then VRP, then
               fwd_ret_20d on the VRP rows written
4. canonical — canonical structure PnL, symbol by symbol, over the daily job's window

``--derive-only`` skips 1–2: they are done once the purge has run, and re-running
the reprojection rewrites every vendor row for nothing. ``--canonical-only`` runs 4
alone (after a change to canonical PnL itself, such as the 0.117.0 entry phase).

New fossils cannot arise: the projection now requires a snapshot row to have been
fetched within ``SNAPSHOT_MAX_FETCH_LAG_DAYS`` of its session.

Usage::

    python -m bifrost_research.engines.volatility.iv_history_repair
    python -m bifrost_research.engines.volatility.iv_history_repair --apply
    python -m bifrost_research.engines.volatility.iv_history_repair --derive-only --apply
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
from bifrost_research.engines.canonical_pnl import run_cohort as run_canonical_cohort
from bifrost_research.engines.volatility.atm_iv import compute_atm_iv_for_date
from bifrost_research.engines.volatility.iv_percentile import compute_iv_percentile_for_date
from bifrost_research.engines.volatility.iv_solver import observed_near_session, project_vendor_snapshot_window
from bifrost_research.engines.vrp.compute import compute_vrp_for_date
from bifrost_research.engines.vrp.entry import backfill_fwd_ret_20d
from bifrost_research.schema.schemas import TABLE_OPTION_IV_RECONSTRUCTED_DAILY as RECON
from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY as CANONICAL

logger = logging.getLogger(__name__)

FIRST_OBSERVATION = date(2026, 8, 5)  # min(snapshot_ts) in raw once P3 re-stamped it
P3_CUTOVER = date(2026, 9, 9)  # sessions from here on were only ever projected post-P3
P3_CUTOVER_TS = datetime(2026, 9, 9, tzinfo=timezone.utc)  # projections after this read observation time
CANONICAL_WINDOW_MONTHS = 6  # runners.run_canonical_pnl: the window the daily job maintains

_UNCONFIRMED = """
    r.solver_status = 'vendor_snapshot'
    AND r.trade_date >= %s AND r.trade_date < %s
    AND r.computed_at < %s
"""
# Dry-run counts, while raw still holds the first observation. After a reprojection
# the unconfirmed rows are exactly these two: never observed on their session, or
# observed but rejected by today's projection (IV missing or out of range, no stock
# close, no contract row) — 9,219 of the latter in the week of 2026-09-06 on DEV.
_OBSERVED = f"""
    EXISTS (
        SELECT 1 FROM raw_market.option_snapshot os
        WHERE os.option_ticker = r.option_ticker
          AND os.snapshot_ts >= (r.trade_date::timestamp AT TIME ZONE 'America/New_York')
          AND os.snapshot_ts < ((r.trade_date + 1)::timestamp AT TIME ZONE 'America/New_York')
          AND {observed_near_session("os")})
"""
_REJECTED = f"""
    EXISTS (
        SELECT 1 FROM raw_market.v_option_snapshot_with_stock os
        LEFT JOIN raw_market.option_contract oc ON oc.option_ticker = os.option_ticker
        WHERE os.option_ticker = r.option_ticker
          AND os.snapshot_ts >= (r.trade_date::timestamp AT TIME ZONE 'America/New_York')
          AND os.snapshot_ts < ((r.trade_date + 1)::timestamp AT TIME ZONE 'America/New_York')
          AND {observed_near_session("os")}
          AND (os.iv IS NULL OR os.iv <= 0 OR os.underlying_price IS NULL OR oc.option_ticker IS NULL
               OR (CASE WHEN os.iv > 3 THEN os.iv / 100 ELSE os.iv END) NOT BETWEEN 0.01 AND 5.0))
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


def _raw_covers_first_observation(conn: Any) -> bool:
    first = _scalar(
        conn,
        "SELECT MIN(DATE(timezone('America/New_York', snapshot_ts))) FROM raw_market.option_snapshot",
    )
    return first is not None and first <= FIRST_OBSERVATION


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


def purge_unconfirmed(conn: Any, *, apply: bool, reprojected_at: datetime | None = None) -> dict[str, Any]:
    """Delete vendor rows dated before P3 that no post-P3 projection has confirmed.

    Applying needs ``reprojected_at``, the start of a reprojection that finished in the
    same run: a week raw observes must then hold rows stamped after it, or the purge
    stops. A dry run (no reprojection yet) counts what the reprojection will leave.
    """
    if apply and reprojected_at is None:
        raise RuntimeError("purge needs a reprojection in the same run")
    covers = _raw_covers_first_observation(conn)
    oldest = _scalar(conn, f"SELECT MIN(trade_date) FROM {RECON} WHERE solver_status = 'vendor_snapshot'")
    if oldest is None or oldest >= P3_CUTOVER:
        return {"step": "purge", "rows": 0, "applied": apply}
    if not apply and not covers:
        return {"step": "purge", "rows": None, "applied": False, "note": "raw no longer holds the first observation; only an applied run can count"}
    chunks = []
    total = 0
    for lo, hi in windows(oldest, P3_CUTOVER - timedelta(days=1), 7):
        params = (lo, hi + timedelta(days=1), P3_CUTOVER_TS)
        if not apply:
            n = int(
                _scalar(conn, f"SELECT COUNT(*) FROM {RECON} r WHERE {_UNCONFIRMED} AND (NOT {_OBSERVED} OR {_REJECTED})", params)
                or 0
            )
        else:
            n = int(_scalar(conn, f"SELECT COUNT(*) FROM {RECON} r WHERE {_UNCONFIRMED}", params) or 0)
            if n and covers and hi >= FIRST_OBSERVATION:
                fresh = _scalar(
                    conn,
                    f"""SELECT COUNT(*) FROM {RECON}
                        WHERE solver_status = 'vendor_snapshot' AND trade_date >= %s AND trade_date < %s
                          AND computed_at >= %s""",
                    (lo, hi + timedelta(days=1), reprojected_at),
                )
                if not fresh:
                    raise RuntimeError(f"purge {lo}..{hi}: raw observes this week but nothing in it was reprojected")
            if n:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {RECON} r WHERE {_UNCONFIRMED}", params)
                    deleted = cur.rowcount
                if deleted != n:
                    conn.rollback()
                    raise RuntimeError(f"purge {lo}..{hi}: counted {n}, delete touched {deleted}; rolled back")
                conn.commit()
        if n:
            chunks.append({"from": lo.isoformat(), "to": hi.isoformat(), "rows": n})
        total += n
    return {"step": "purge", "rows": total, "applied": apply, "chunks": chunks}


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


def rebuild_canonical_pnl(conn: Any, symbols: Sequence[str], end: date, *, apply: bool) -> dict[str, Any]:
    """Canonical structure PnL over the daily job's window, one symbol at a time: its
    rows are deleted and recomputed in one pass, so a reader sees at most one symbol
    missing. Rows priced on the old IV go with them, including entries older than the
    window that no run maintained (2025-09 → 2026-03 on DEV; no reader goes back
    that far — hypotheses start 2026-08-29)."""
    if not apply:
        n = _scalar(conn, f"SELECT COUNT(*) FROM {CANONICAL}")
        return {"step": "canonical_pnl", "rows_now": int(n or 0), "symbols": len(symbols), "applied": False}
    written = 0
    for sym in symbols:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {CANONICAL} WHERE symbol = %s", (sym,))
        out = run_canonical_cohort(
            conn, symbols=[sym], lookback_months=CANONICAL_WINDOW_MONTHS, as_of=end, coverage=False
        )
        conn.commit()  # a symbol with no marks still owes its delete
        written += int(out.get("rows_written") or 0)
    return {"step": "canonical_pnl", "rows_written": written, "symbols": len(symbols), "applied": True}


def run(
    *, start: date | None, end: date, apply: bool, derive_only: bool = False, canonical_only: bool = False
) -> dict[str, Any]:
    conn = connect()
    try:
        universe = union_iv_radar_benchmarks(load_symbols_from_env_or_query(conn))
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT symbol FROM {RECON} WHERE solver_status = 'vendor_snapshot' AND trade_date < %s",
                (P3_CUTOVER,),
            )
            projected = sorted({*universe, *(str(r[0]) for r in cur.fetchall())})
        if start is None:
            firsts = (
                _scalar(conn, "SELECT MIN(trade_date) FROM features.option_metric_atm_iv_daily"),
                _scalar(conn, "SELECT MIN(bar_date) FROM raw_market.option_daily"),
                FIRST_OBSERVATION,
            )
            start = min(d for d in firsts if d is not None)
        steps: list[dict[str, Any]] = []
        if canonical_only:
            with conn.cursor() as cur:
                cur.execute(f"SELECT DISTINCT symbol FROM {CANONICAL}")
                canonical_syms = sorted({*universe, *(str(r[0]) for r in cur.fetchall())})
            steps.append(rebuild_canonical_pnl(conn, canonical_syms, end, apply=apply))
            return {"start": start.isoformat(), "end": end.isoformat(), "universe": len(universe), "steps": steps}
        if not derive_only:
            reprojected_at = datetime.now(timezone.utc)
            steps.append(reproject_vendor(conn, projected, end, apply=apply))
            steps.append(purge_unconfirmed(conn, apply=apply, reprojected_at=reprojected_at if apply else None))
        days = sessions(conn, start, end)
        steps.append(derive(conn, days, universe, apply=apply, progress=lambda d: logger.info("derived %s", d)))
        if apply:
            steps.append({"step": "fwd_ret_20d", **backfill_fwd_ret_20d(lookback_days=(end - start).days + 1, as_of=end)})
        with conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT symbol FROM {CANONICAL}")
            canonical_syms = sorted({*universe, *(str(r[0]) for r in cur.fetchall())})
        steps.append(rebuild_canonical_pnl(conn, canonical_syms, end, apply=apply))
        return {"start": start.isoformat(), "end": end.isoformat(), "universe": len(universe), "steps": steps}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair IV history left by the pre-P3 snapshot model")
    parser.add_argument("--start", default=None, help="YYYY-MM-DD (default: oldest ATM row / option_daily bar)")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD (default: today in New York)")
    parser.add_argument("--apply", action="store_true", help="write; without it every step only counts")
    parser.add_argument("--derive-only", action="store_true", help="skip reproject and purge (done once already)")
    parser.add_argument("--canonical-only", action="store_true", help="rebuild canonical PnL only")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    today_ny = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
    result = run(
        start=date.fromisoformat(args.start) if args.start else None,
        end=date.fromisoformat(args.end) if args.end else today_ny,
        apply=args.apply,
        derive_only=args.derive_only,
        canonical_only=args.canonical_only,
    )
    print(json.dumps(result, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
