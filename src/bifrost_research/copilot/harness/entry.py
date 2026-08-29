"""CLI entry for harness CronJob — Wave A.

Usage:
  python -m bifrost_research.copilot.harness.entry --objective-id=obj-...
  python -m bifrost_research.copilot.harness.entry --schedule=daily_open
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from bifrost_research.copilot.harness.runtime import run_objective
from bifrost_research.db.conn import connect
from bifrost_research.repositories import objective as obj_repo

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("harness.entry")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Research harness objective(s)")
    parser.add_argument("--objective-id", default=None)
    parser.add_argument("--schedule", default=None, help="Filter active objectives by schedule")
    parser.add_argument("--dry-list", action="store_true", help="List matching objectives and exit")
    args = parser.parse_args(argv)

    conn = connect()
    try:
        if args.objective_id:
            objectives = [obj_repo.get_objective(conn, args.objective_id)]
            if objectives[0] is None:
                logger.error("objective not found: %s", args.objective_id)
                return 1
        else:
            objectives = obj_repo.list_objectives(conn, status="active", limit=50)
            if args.schedule:
                objectives = [o for o in objectives if o.get("schedule") == args.schedule]

        if args.dry_list:
            print(json.dumps(objectives, indent=2, default=str))
            return 0

        if not objectives:
            logger.info("no objectives to run")
            return 0

        for obj in objectives:
            logger.info("running objective %s (%s)", obj["id"], obj.get("title"))
            result = run_objective(conn, objective=obj)
            print(json.dumps(result, indent=2, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
