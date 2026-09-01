"""CLI entry for harness CronJob — Wave A + LO-0…LO-4 Loop Orchestrator.

Usage:
  python -m bifrost_research.copilot.harness.entry --objective-id=obj-...
  python -m bifrost_research.copilot.harness.entry --schedule=daily_open
  python -m bifrost_research.copilot.harness.entry --schedule=daily_open --batch-mode
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from bifrost_research.copilot.harness import data_sources as ds
from bifrost_research.copilot.harness import readiness as readiness_mod
from bifrost_research.copilot.harness.batch import RESEARCH_AUTO_APPROVE_KINDS, approve_all_for_run
from bifrost_research.copilot.harness.runtime import run_objective
from bifrost_research.copilot.harness.trust_gate import trust_l0_research_loop_batch
from bifrost_research.copilot.curator.runtime import run_curator_for_run
from bifrost_research.db.conn import connect
from bifrost_research.repositories import objective as obj_repo

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("harness.entry")


def _check_sepa_fresh(conn, max_stale_days: int) -> int:
    ok, msg = readiness_mod.check_sepa_fresh(conn, max_stale_days)
    if not ok:
        logger.warning("sepa freshness: %s", msg)
        return 2
    logger.info("sepa freshness: %s", msg)
    return 0


def _check_scan_fresh(conn, max_stale_days: int) -> int:
    stale = ds.scan_stale_days(conn)
    if stale is None:
        logger.warning("scan freshness: no stock_signal_scan_daily rows")
        return 2
    if stale > max_stale_days:
        logger.warning(
            "scan freshness: latest snapshot is %d days old (max %d)",
            stale,
            max_stale_days,
        )
        return 2
    logger.info("scan freshness ok (%d days old)", stale)
    return 0


def _process_objective(
    conn,
    obj: dict,
    *,
    curate_after: bool,
    batch_mode: bool,
) -> dict:
    policy = obj.get("policy_json") or {}
    auto_validate = bool(policy.get("auto_validate", True))

    result = run_objective(conn, objective=obj)
    run = result.get("run") or {}
    run_id = str(run.get("id") or "")
    if not run_id:
        return result

    if curate_after and run.get("status") == "awaiting_approval":
        logger.info("CuratorRun for %s", run_id)
        try:
            curate_result = run_curator_for_run(conn, run_id, skip_agent=False)
            result["curator"] = curate_result
        except Exception as exc:
            logger.exception("curate failed")
            result["curator_error"] = str(exc)

    if batch_mode and run.get("status") == "awaiting_approval":
        if trust_l0_research_loop_batch():
            if curate_after and "curator" not in result and "curator_error" not in result:
                try:
                    result["curator"] = run_curator_for_run(conn, run_id)
                except Exception as exc:
                    logger.warning("batch curate skipped: %s", exc)
            logger.info("batch auto-approve for %s", run_id)
            result["approve_all"] = approve_all_for_run(
                conn,
                run_id,
                approved_by="system:loop_batch",
                owner_id=str(obj.get("owner_id") or "owner"),
                kinds_whitelist=RESEARCH_AUTO_APPROVE_KINDS,
                auto_validate=auto_validate,
            )
        else:
            logger.info("batch mode on but research-loop-batch not L0 — skip auto-approve")

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Research harness objective(s)")
    parser.add_argument("--objective-id", default=None)
    parser.add_argument("--schedule", default=None, help="Filter active objectives by schedule")
    parser.add_argument("--dry-list", action="store_true", help="List matching objectives and exit")
    parser.add_argument(
        "--require-sepa-fresh",
        action="store_true",
        help="Exit 2 when features.stock_signal_sepa_daily is stale",
    )
    parser.add_argument(
        "--sepa-max-stale-days",
        type=int,
        default=3,
        help="Max calendar days since latest SEPA model snapshot",
    )
    parser.add_argument(
        "--require-scan-fresh",
        action="store_true",
        help="Exit 2 when features.stock_signal_scan_daily is stale",
    )
    parser.add_argument(
        "--scan-max-stale-days",
        type=int,
        default=3,
        help="Max calendar days since latest scan snapshot (with --require-scan-fresh)",
    )
    parser.add_argument(
        "--curate-after",
        action="store_true",
        help="Run headless CuratorRun after harness when awaiting_approval",
    )
    parser.add_argument(
        "--batch-mode",
        action="store_true",
        help="When Trust L0: curate + auto-approve research drafts + validate hooks",
    )
    args = parser.parse_args(argv)

    conn = connect()
    exit_code = 0
    try:
        if args.require_sepa_fresh:
            code = _check_sepa_fresh(conn, args.sepa_max_stale_days)
            if code != 0:
                return code
        if args.require_scan_fresh:
            code = _check_scan_fresh(conn, args.scan_max_stale_days)
            if code != 0:
                return code

        if args.objective_id:
            objectives = [obj_repo.get_objective(conn, args.objective_id)]
            if objectives[0] is None:
                logger.error("objective not found: %s", args.objective_id)
                return 1
        else:
            env_oid = os.environ.get("BIFROST_LOOP_OBJECTIVE_ID", "").strip()
            if env_oid:
                obj = obj_repo.get_objective(conn, env_oid)
                objectives = [obj] if obj else []
                if not obj:
                    logger.error("BIFROST_LOOP_OBJECTIVE_ID not found: %s", env_oid)
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
            result = _process_objective(
                conn,
                obj,
                curate_after=args.curate_after or args.batch_mode,
                batch_mode=args.batch_mode,
            )
            print(json.dumps(result, indent=2, default=str))
    finally:
        conn.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
