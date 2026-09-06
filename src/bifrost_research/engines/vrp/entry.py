"""VRP compute CronJob entrypoint — Wave RS-B-VRP1.

Usage::

    python -m bifrost_research.engines.vrp.entry
    python -m bifrost_research.engines.vrp.entry --symbol NVDA --dry-run
    python -m bifrost_research.engines.vrp.entry --as-of 2026-08-22
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

from bifrost_research.db.calendar import (
    fetch_recent_trading_days,
    load_symbols_from_env_or_query,
    union_iv_radar_benchmarks,
)
from bifrost_research.db.conn import connect
from bifrost_research.engines.vrp.compute import (
    compute_fwd_ret_20d,
    compute_vrp_for_date,
    compute_vrp_for_symbol,
)

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")
# 20 sessions is about 28 calendar days; wait a little longer before trying a row.
MIN_AGE_DAYS_FOR_20D = 30


def _today_ny() -> date:
    return datetime.now(timezone.utc).astimezone(_NY).date()


def run(
    *,
    lookback_days: int = 3,
    symbols: Sequence[str] | None = None,
    as_of: date | None = None,
    dry_run: bool = False,
    single_symbol: str | None = None,
) -> dict[str, object]:
    day = as_of or _today_ny()
    conn = connect()
    try:
        underlyings = load_symbols_from_env_or_query(conn, symbols=symbols)
        universe = union_iv_radar_benchmarks(underlyings)

        if single_symbol:
            sym = single_symbol.strip().upper()
            row = compute_vrp_for_symbol(conn, symbol=sym, trade_date=day)
            return {
                "mode": "single-symbol",
                "symbol": sym,
                "trade_date": day.isoformat(),
                "row": row,
                "dry_run": dry_run,
            }

        trading_days = fetch_recent_trading_days(conn, lookback_days, as_of=day)
        if not trading_days:
            return {
                "mode": "batch",
                "lookback_days": lookback_days,
                "skipped": True,
                "reason": "no trading days",
            }

        if dry_run:
            # Emit one representative row per available day without writing.
            sample_sym = universe[0] if universe else "SPY"
            preview_rows = []
            for td in trading_days:
                r = compute_vrp_for_symbol(conn, symbol=sample_sym, trade_date=td)
                preview_rows.append({"trade_date": td.isoformat(), "row": r})
            return {
                "mode": "dry-run",
                "symbol": sample_sym,
                "lookback_days": lookback_days,
                "preview": preview_rows,
            }

        totals = {"rows_written": 0, "skipped": 0, "days": []}
        for td in trading_days:
            one = compute_vrp_for_date(
                conn,
                trade_date=td,
                underlyings=universe or None,
            )
            totals["rows_written"] += int(one.get("rows_written") or 0)
            totals["skipped"] += int(one.get("skipped") or 0)
            totals["days"].append(one)
        result: dict[str, object] = {
            "mode": "batch",
            "lookback_days": lookback_days,
            "symbols": len(universe),
            "trading_days": [d.isoformat() for d in trading_days],
            **totals,
        }
        logger.info(
            "vrp days=%s..%s rows=%s skipped=%s",
            trading_days[0],
            trading_days[-1],
            totals["rows_written"],
            totals["skipped"],
        )
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def backfill_fwd_ret_20d(*, lookback_days: int = 90, as_of: date | None = None) -> dict[str, object]:
    """Fill ``fwd_ret_20d`` on VRP rows whose 20 sessions have since elapsed.

    The daily compute leaves the column NULL on purpose (the future is not known
    on the day). This walks the last ``lookback_days`` of rows that are still
    NULL and old enough, and writes the log return from the close series.
    """
    day = as_of or _today_ny()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, trade_date
                FROM features.stock_signal_vrp_daily
                WHERE fwd_ret_20d IS NULL
                  AND trade_date >= %s
                  AND trade_date <= %s
                ORDER BY symbol, trade_date
                """,
                (day - timedelta(days=lookback_days), day - timedelta(days=MIN_AGE_DAYS_FOR_20D)),
            )
            pending = [(str(r[0]).upper(), r[1]) for r in (cur.fetchall() or [])]
        by_symbol: dict[str, list[date]] = {}
        for sym, td in pending:
            by_symbol.setdefault(sym, []).append(td)
        updated = 0
        still_pending = 0
        for sym, dates in by_symbol.items():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT bar_date, close::float
                    FROM raw_market.stock_daily
                    WHERE symbol = %s AND bar_date >= %s AND close IS NOT NULL AND close > 0
                    ORDER BY bar_date ASC
                    """,
                    (sym, min(dates)),
                )
                pairs = [(r[0], float(r[1])) for r in (cur.fetchall() or [])]
            for td in dates:
                fwd = compute_fwd_ret_20d(pairs, trade_date=td)
                if fwd is None:
                    still_pending += 1
                    continue
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE features.stock_signal_vrp_daily
                        SET fwd_ret_20d = %s
                        WHERE symbol = %s AND trade_date = %s
                        """,
                        (fwd, sym, td),
                    )
                updated += 1
        conn.commit()
        result: dict[str, object] = {
            "mode": "fwd_ret_20d",
            "lookback_days": lookback_days,
            "candidates": len(pending),
            "updated": updated,
            "still_pending": still_pending,
        }
        logger.info("vrp fwd_ret_20d backfill=%s", result)
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Research VRP compute (Wave RS-B-VRP1)")
    parser.add_argument("--lookback-days", type=int, default=3)
    parser.add_argument("--as-of", type=str, default="", help="YYYY-MM-DD (NY session)")
    parser.add_argument("--symbol", type=str, default="", help="Single-symbol run (skips batch)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    result = run(
        lookback_days=args.lookback_days,
        as_of=as_of,
        dry_run=args.dry_run,
        single_symbol=args.symbol or None,
    )
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
