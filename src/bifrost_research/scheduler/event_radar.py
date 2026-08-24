"""Event Radar file-ingest CronJob entrypoint.

Owner decision A (research-radar-news-source):
  Research-workspace input/ → Cron → pipeline → features_signals.event_radar

Env:
  EVENT_RADAR_INPUT_DIR   default /data/event-radar/input
  EVENT_RADAR_ARCHIVE_DIR default <input_parent>/archive
  EVENT_RADAR_ARCHIVE     default 1 (move processed files)

D10 BLOCKED / D13 OLAP-only.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from typing import Sequence

from bifrost_research.engines.event_radar.ingest import ingest_directory

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Event Radar file ingest")
    parser.add_argument(
        "--input-dir",
        default=None,
        help="Override EVENT_RADAR_INPUT_DIR",
    )
    parser.add_argument(
        "--archive-dir",
        default=None,
        help="Override EVENT_RADAR_ARCHIVE_DIR",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Leave files in input/ after processing",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline without DB upsert or archive",
    )
    parser.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD collected_at")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    collected = date.fromisoformat(args.as_of) if args.as_of else None

    try:
        summary = ingest_directory(
            args.input_dir,
            archive_dir=args.archive_dir,
            collected_at=collected,
            upsert=not args.dry_run,
            archive=False if args.dry_run or args.no_archive else None,
        )
    except Exception:
        logger.exception("event_radar ingest failed")
        return 1

    logger.info("result=%s", summary.to_dict())
    return 0 if summary.files_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
