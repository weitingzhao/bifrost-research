"""Historical IV Solver CronJob / one-shot Job entrypoint — IDS.

Usage::

    python -m bifrost_research.engines.volatility.iv_solver_entry
    python -m bifrost_research.engines.volatility.iv_solver_entry --symbol SPY --lookback-days 30 --dry-run
    python -m bifrost_research.engines.volatility.iv_solver_entry --source snapshot --lookback-days 252
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from bifrost_research.db.calendar import load_symbols_from_env_or_query, union_iv_radar_benchmarks
from bifrost_research.db.conn import connect
from bifrost_research.engines.volatility.iv_solver import run_cohort
from bifrost_research.schema.ddl import apply_features_ddl

logger = logging.getLogger(__name__)
_NY = ZoneInfo("America/New_York")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Historical IV solver dual-source backfill")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.environ.get("RESEARCH_LOOKBACK_DAYS") or 252),
    )
    parser.add_argument("--symbol", type=str, default=None)
    parser.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument(
        "--source",
        choices=("all", "daily", "snapshot"),
        default="all",
        help="all=vendor snapshot + OHLCV Brent; snapshot|daily for one source",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ensure-ddl", action="store_true", default=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    as_of = (
        date.fromisoformat(args.as_of)
        if args.as_of
        else datetime.now(timezone.utc).astimezone(_NY).date()
    )

    conn = connect()
    try:
        if args.ensure_ddl:
            try:
                apply_features_ddl(conn)
            except Exception as exc:
                # analytics_writer often lacks CREATE on features (bifrost-owned DDL).
                # Tables already exist in Golden Source — continue the solve.
                logger.warning("ensure-ddl skipped: %s", exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
        underlyings = load_symbols_from_env_or_query(conn)
        universe = union_iv_radar_benchmarks(underlyings)
        if args.symbol:
            universe = [args.symbol.strip().upper()]
        result = run_cohort(
            conn,
            symbols=universe,
            lookback_days=args.lookback_days,
            as_of=as_of,
            source=args.source,  # type: ignore[arg-type]
            dry_run=args.dry_run,
        )
        print(json.dumps(result, default=str, indent=2))
        return 0
    except Exception:
        logger.exception("iv_solver entry failed")
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
