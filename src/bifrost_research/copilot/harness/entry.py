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
from bifrost_research.copilot.harness.batch_orchestrate import process_objective
from bifrost_research.copilot.harness.trust_report import report_batch_outcome
from bifrost_research.db.conn import connect, rollback_quietly
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
    return process_objective(
        conn,
        obj,
        curate_after=curate_after,
        batch_mode=batch_mode,
    )


# B4 (research-loop-automation): every objective runs, and one objective's
# failure is that objective's, not the batch's. Before this the loop raised on
# the first exception, so a second objective never ran while the first was
# broken — and the Cron only ever ran one objective anyway.


def objective_outcome(
    obj: dict,
    *,
    result: dict | None = None,
    error: BaseException | None = None,
) -> dict:
    """One line's worth of facts about one objective's run."""
    out: dict = {
        "id": str(obj.get("id") or ""),
        "title": str(obj.get("title") or ""),
        "ok": error is None,
    }
    if error is not None:
        out["error"] = f"{type(error).__name__}: {str(error)[:200]}"
        return out
    result = result or {}
    run = result.get("run") if isinstance(result.get("run"), dict) else {}
    outputs = result.get("outputs") if isinstance(result.get("outputs"), dict) else {}
    out["run_id"] = run.get("id")
    out["status"] = run.get("status")
    out["candidates"] = len(outputs.get("candidate_ids") or [])
    out["approved"] = bool(result.get("approve_result")) and not result.get("approve_skipped")
    return out


def summary_line(outcome: dict) -> str:
    head = f"objective {outcome.get('id')} ({outcome.get('title')})"
    if not outcome.get("ok"):
        return f"{head}: FAILED {outcome.get('error')}"
    return (
        f"{head}: ok run={outcome.get('run_id')} status={outcome.get('status')} "
        f"candidates={outcome.get('candidates')}"
        + (" auto-approved" if outcome.get("approved") else "")
    )


def batch_summary(outcomes: list[dict]) -> str:
    ok = [o for o in outcomes if o.get("ok")]
    failed = [o for o in outcomes if not o.get("ok")]
    text = f"{len(ok)}/{len(outcomes)} objectives ok"
    if failed:
        text += "; failed: " + ", ".join(f"{o.get('id')} ({o.get('error')})" for o in failed)
    return text


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

        outcomes: list[dict] = []
        for obj in objectives:
            logger.info("running objective %s (%s)", obj["id"], obj.get("title"))
            try:
                result = _process_objective(
                    conn,
                    obj,
                    curate_after=args.curate_after or args.batch_mode,
                    batch_mode=args.batch_mode,
                )
            except Exception as exc:  # noqa: BLE001
                # This objective's failure, logged with its traceback; the
                # transaction is rolled back so the next objective starts clean.
                logger.exception("objective %s failed", obj.get("id"))
                rollback_quietly(conn)
                outcomes.append(objective_outcome(obj, error=exc))
                continue
            outcomes.append(objective_outcome(obj, result=result))
            print(json.dumps(result, indent=2, default=str))

        for outcome in outcomes:
            logger.info(summary_line(outcome))
        # One report per invocation of the skill, whichever way it ended — a
        # matrix that only ever hears about successes cannot demote.
        if args.batch_mode:
            report_batch_outcome(
                ok=all(o["ok"] for o in outcomes), summary=batch_summary(outcomes)
            )
        # Non-zero only when nothing ran: a retry would re-run the objectives
        # that did succeed and propose their batches twice.
        if outcomes and not any(o["ok"] for o in outcomes):
            exit_code = 1
    finally:
        conn.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
