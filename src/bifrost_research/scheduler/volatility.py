"""Volatility compute scheduler — CronJob entrypoint for Research NS."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from bifrost_research.db.calendar import (
    fetch_recent_trading_days,
    load_symbols_from_env_or_query,
    union_iv_radar_benchmarks,
)
from bifrost_research.db.conn import connect
from bifrost_research.engines.volatility.atm_iv import compute_atm_iv_for_date
from bifrost_research.engines.volatility.iv_percentile import (
    DEFAULT_PERCENTILE_WINDOW,
    compute_iv_percentile_for_date,
)
from bifrost_research.engines.volatility.max_pain import compute_max_pain_for_date
from bifrost_research.engines.volatility.pcr import compute_pcr_for_date

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")

SLOT_NAMES = ("max-pain", "atm-iv-pcr", "iv-percentile")


def _today_ny() -> date:
    return datetime.now(timezone.utc).astimezone(_NY).date()


def run_slot(
    slot: str,
    *,
    lookback_days: int = 3,
    percentile_window: int = DEFAULT_PERCENTILE_WINDOW,
    symbols: Sequence[str] | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Run one volatility compute slot against golden_source."""
    day = as_of or _today_ny()
    conn = connect()
    try:
        underlyings = load_symbols_from_env_or_query(conn, symbols=symbols)
        trading_days = fetch_recent_trading_days(conn, lookback_days, as_of=day)
        if not trading_days:
            return {
                "slot": slot,
                "lookback_days": lookback_days,
                "skipped": True,
                "reason": "no trading days",
            }

        if slot == "max-pain":
            day_results = []
            total_written = 0
            total_groups = 0
            for td in trading_days:
                one = compute_max_pain_for_date(
                    conn,
                    trade_date=td,
                    underlyings=underlyings or None,
                )
                day_results.append(one)
                total_written += int(one.get("rows_written") or 0)
                total_groups += int(one.get("groups") or 0)
            result = {
                "slot": slot,
                "lookback_days": lookback_days,
                "symbols": len(underlyings),
                "trading_days": [d.isoformat() for d in trading_days],
                "groups": total_groups,
                "rows_written": total_written,
                "days": day_results,
            }
            logger.info(
                "max-pain days=%s..%s groups=%s rows=%s",
                trading_days[0],
                trading_days[-1],
                total_groups,
                total_written,
            )
            return result

        if slot == "atm-iv-pcr":
            syms = union_iv_radar_benchmarks(underlyings)
            atm_written = 0
            pcr_written = 0
            atm_days = []
            pcr_days = []
            for td in trading_days:
                atm_one = compute_atm_iv_for_date(conn, trade_date=td, underlyings=syms or None)
                pcr_one = compute_pcr_for_date(conn, trade_date=td, underlyings=syms or None)
                atm_days.append(atm_one)
                pcr_days.append(pcr_one)
                atm_written += int(atm_one.get("rows_written") or 0)
                pcr_written += int(pcr_one.get("rows_written") or 0)
            result = {
                "slot": slot,
                "lookback_days": lookback_days,
                "symbols": len(syms),
                "trading_days": [d.isoformat() for d in trading_days],
                "atm_rows_written": atm_written,
                "pcr_rows_written": pcr_written,
                "atm_days": atm_days,
                "pcr_days": pcr_days,
            }
            logger.info(
                "atm-iv-pcr days=%s..%s atm_rows=%s pcr_rows=%s",
                trading_days[0],
                trading_days[-1],
                atm_written,
                pcr_written,
            )
            return result

        if slot == "iv-percentile":
            syms = union_iv_radar_benchmarks(underlyings)
            day_results = []
            total_written = 0
            for td in trading_days:
                one = compute_iv_percentile_for_date(
                    conn,
                    trade_date=td,
                    underlyings=syms or None,
                    percentile_window=percentile_window,
                )
                day_results.append(one)
                total_written += int(one.get("rows_written") or 0)
            result = {
                "slot": slot,
                "lookback_days": lookback_days,
                "percentile_window": percentile_window,
                "symbols": len(syms),
                "trading_days": [d.isoformat() for d in trading_days],
                "rows_written": total_written,
                "days": day_results,
            }
            logger.info(
                "iv-percentile days=%s..%s rows=%s",
                trading_days[0],
                trading_days[-1],
                total_written,
            )
            return result

        raise ValueError(f"unknown slot: {slot!r}; expected one of {SLOT_NAMES}")
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
    parser = argparse.ArgumentParser(description="Research volatility compute scheduler")
    parser.add_argument("--slot", required=True, choices=SLOT_NAMES)
    parser.add_argument("--lookback-days", type=int, default=3)
    parser.add_argument("--percentile-window", type=int, default=DEFAULT_PERCENTILE_WINDOW)
    parser.add_argument("--as-of", type=str, default="", help="YYYY-MM-DD (NY session)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    result = run_slot(
        args.slot,
        lookback_days=args.lookback_days,
        percentile_window=args.percentile_window,
        as_of=as_of,
    )
    print(result)
    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
