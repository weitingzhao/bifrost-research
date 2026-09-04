"""Shared harness batch orchestration — CLI entry + HTTP batch-run.

D10 BLOCKED — research drafts only; never auto-approves policy_suggestion /
order_intent (narrow whitelist via RESEARCH_AUTO_APPROVE_KINDS).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from bifrost_research.copilot.harness.batch import RESEARCH_AUTO_APPROVE_KINDS, approve_all_for_run
from bifrost_research.copilot.harness.runtime import run_objective
from bifrost_research.copilot.harness.trust_gate import (
    SKILL_ID,
    batch_mode_enabled,
    trust_l0_research_loop_batch,
)
from bifrost_research.copilot.curator.runtime import run_curator_for_run
from bifrost_research.repositories import objective as obj_repo

logger = logging.getLogger(__name__)


def trust_status() -> dict[str, Any]:
    """Observable Trust gate for Console / API."""
    env_on = batch_mode_enabled()
    override = os.environ.get("BIFROST_LOOP_TRUST_L0_OVERRIDE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    l0 = trust_l0_research_loop_batch()
    reason = "ok"
    if not env_on and not override:
        reason = "BIFROST_LOOP_BATCH_MODE not set — auto-approve disabled"
    elif not l0:
        reason = (
            "research-loop-batch not at Trust L0 "
            "(promote in Ops Console or set BIFROST_LOOP_TRUST_L0_OVERRIDE=1)"
        )
    return {
        "skill": SKILL_ID,
        "batch_mode_env": env_on,
        "trust_l0_override": override,
        "l0": l0,
        "reason": reason,
        "advisory": "D10 BLOCKED — auto-approve is research drafts only, never orders",
    }


def _append_batch_event(
    conn: Any,
    run_id: str,
    step: str,
    *,
    label: str,
    detail: str = "",
    decision: str = "",
) -> None:
    try:
        obj_repo.append_run_trace_event(
            conn,
            run_id,
            {
                "step": step,
                "label": label,
                "detail": detail,
                "decision": decision,
            },
            progress={"step": step, "label": label, "detail": detail},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("batch progress flush failed: %s", exc)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass


def process_objective(
    conn: Any,
    obj: dict[str, Any],
    *,
    curate_after: bool,
    batch_mode: bool,
    existing_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run harness → optional curator → optional Trust-gated approve-all."""
    policy = obj.get("policy_json") or {}
    auto_validate = bool(policy.get("auto_validate", True))
    trust = trust_status()

    result = run_objective(conn, objective=obj, existing_run=existing_run)
    result["trust"] = trust
    run = result.get("run") or {}
    run_id = str(run.get("id") or "")
    if not run_id:
        return result

    if curate_after and run.get("status") == "awaiting_approval":
        logger.info("CuratorRun for %s", run_id)
        try:
            curate_result = run_curator_for_run(conn, run_id, skip_agent=False)
            result["curator"] = curate_result
            _append_batch_event(
                conn,
                run_id,
                "curate",
                label="Curator",
                detail="Headless loop_curator finished",
                decision="curator_ok",
            )
        except Exception as exc:
            logger.exception("curate failed")
            result["curator_error"] = str(exc)
            _append_batch_event(
                conn,
                run_id,
                "curate",
                label="Curator",
                detail=str(exc)[:200],
                decision="curator_error",
            )

    if batch_mode and run.get("status") == "awaiting_approval":
        if trust_l0_research_loop_batch():
            if curate_after and "curator" not in result and "curator_error" not in result:
                try:
                    result["curator"] = run_curator_for_run(conn, run_id)
                except Exception as exc:
                    logger.warning("batch curate skipped: %s", exc)
            logger.info("batch auto-approve for %s", run_id)
            approve_result = approve_all_for_run(
                conn,
                run_id,
                approved_by="system:loop_batch",
                owner_id=str(obj.get("owner_id") or "owner"),
                kinds_whitelist=RESEARCH_AUTO_APPROVE_KINDS,
                auto_validate=auto_validate,
            )
            result["approve_all"] = approve_result
            held = int(approve_result.get("held_count") or 0)
            approved_n = int(approve_result.get("count") or 0)
            step = "held" if approve_result.get("skipped_batch") else "approve_all"
            _append_batch_event(
                conn,
                run_id,
                step,
                label="Auto-approve",
                detail=f"approved={approved_n} held={held}",
                decision=(
                    "skipped_dissent"
                    if approve_result.get("skipped_batch")
                    else f"approved_{approved_n}"
                ),
            )
            try:
                obj_repo.patch_run_outputs(
                    conn,
                    run_id,
                    {
                        "approve_all": {
                            "count": approved_n,
                            "held_count": held,
                            "skipped_batch": bool(approve_result.get("skipped_batch")),
                        },
                        "trust": trust,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("patch approve outputs failed: %s", exc)
            result["run"] = obj_repo.get_run(conn, run_id) or run
        else:
            logger.info("batch mode on but research-loop-batch not L0 — skip auto-approve")
            result["approve_skipped"] = True
            _append_batch_event(
                conn,
                run_id,
                "held",
                label="Auto-approve skipped",
                detail=str(trust.get("reason") or "not Trust L0"),
                decision="trust_not_l0",
            )
            try:
                obj_repo.patch_run_outputs(
                    conn,
                    run_id,
                    {"approve_skipped": True, "trust": trust},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("patch skip outputs failed: %s", exc)
            result["run"] = obj_repo.get_run(conn, run_id) or run

    return result


__all__ = ["process_objective", "trust_status"]
