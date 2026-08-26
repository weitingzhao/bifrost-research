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
from datetime import date, datetime, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

from bifrost_research.db.calendar import (
    fetch_recent_trading_days,
    load_symbols_from_env_or_query,
    union_iv_radar_benchmarks,
)
from bifrost_research.db.conn import connect
from bifrost_research.engines.vrp.compute import (
    compute_vrp_for_date,
    compute_vrp_for_symbol,
)

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")


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
