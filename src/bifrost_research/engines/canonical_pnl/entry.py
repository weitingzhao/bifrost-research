"""Canonical PnL CronJob entrypoint — Wave Canonical-PnL Foundation.

Usage::

    python -m bifrost_research.engines.canonical_pnl.entry
    python -m bifrost_research.engines.canonical_pnl.entry --dry-run
    python -m bifrost_research.engines.canonical_pnl.entry --symbol SPY --lookback-months 3
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from bifrost_research.db.calendar import load_symbols_from_env_or_query, union_iv_radar_benchmarks
from bifrost_research.db.conn import connect
from bifrost_research.engines.canonical_pnl import run_cohort
from bifrost_research.schema.ddl import apply_features_ddl

logger = logging.getLogger(__name__)
_NY = ZoneInfo("America/New_York")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canonical structure PnL cohort compute")
    parser.add_argument("--lookback-months", type=int, default=6)
    parser.add_argument("--entry-stride-days", type=int, default=5)
    parser.add_argument("--symbol", type=str, default=None)
    parser.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="TRUNCATE dual-write tables before cohort (full rebuild)",
    )
    parser.add_argument("--ensure-ddl", action="store_true", default=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc).astimezone(_NY).date()

    conn = connect()
    try:
        if args.ensure_ddl:
            apply_features_ddl(conn)
        underlyings = load_symbols_from_env_or_query(conn)
        universe = union_iv_radar_benchmarks(underlyings)
        if args.symbol:
            universe = [args.symbol.strip().upper()]
        result = run_cohort(
            conn,
            symbols=universe,
            lookback_months=args.lookback_months,
            as_of=as_of,
            entry_stride_days=args.entry_stride_days,
            dry_run=args.dry_run,
            reset=args.reset,
        )
        print(json.dumps(result, default=str, indent=2))
        return 0
    except Exception:
        logger.exception("canonical_pnl entry failed")
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
