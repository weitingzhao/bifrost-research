"""Shared harness batch orchestration — CLI entry + HTTP batch-run.

D10 BLOCKED — research drafts only; never auto-approves policy_suggestion /
order_intent (narrow whitelist via RESEARCH_AUTO_APPROVE_KINDS).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from bifrost_research.copilot.harness.batch import RESEARCH_AUTO_APPROVE_KINDS, approve_all_for_run
from bifrost_research.copilot.harness.leash import DEFAULT_MIN_SOURCE_HIT_RATE
from bifrost_research.copilot.harness.runtime import run_objective
from bifrost_research.copilot.harness.trust_gate import (
    SKILL_ID,
    batch_mode_enabled,
    matrix_level,
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
    level = matrix_level()
    matrix_l0 = level == "L0"
    l0 = trust_l0_research_loop_batch()
    # Two facts, kept apart: what the Owner granted (the matrix) and whether
    # this process may act on it (the gate). The API pod serving the console
    # is never the unattended loop, so its gate is closed by design — and the
    # console pill must report the grant, not this pod's gate.
    if level is None:
        reason = "trust matrix unreachable — grant unknown, gate closed"
    elif not matrix_l0:
        reason = f"research-loop-batch at Trust {level} — promote to L0 in Ops Console"
    elif not env_on and not override:
        reason = (
            "Trust L0 granted; this process is not the unattended loop "
            "(BIFROST_LOOP_BATCH_MODE unset), so it auto-approves nothing"
        )
    else:
        reason = "ok"
    return {
        "skill": SKILL_ID,
        "batch_mode_env": env_on,
        "trust_l0_override": override,
        "matrix_level": level,
        "matrix_l0": matrix_l0,
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


def start_preview(obj: dict[str, Any]) -> dict[str, Any]:
    """What a run of this objective would do — the dry-run of ``research.loop.run_objective``."""
    from bifrost_research.copilot.harness.runtime import _heuristic_plan

    policy = obj.get("policy_json") or {}
    plan = _heuristic_plan(obj)
    return {
        "objective": {
            "id": obj.get("id"),
            "title": obj.get("title"),
            "status": obj.get("status"),
            "universe_mode": policy.get("universe_mode"),
            "max_candidates": policy.get("max_candidates"),
            "persona_evaluate": policy.get("persona_evaluate"),
            "require_validate_pass": policy.get("require_validate_pass"),
            "use_llm_plan": policy.get("use_llm_plan"),
        },
        "plan": {
            "generated_by": plan.get("generated_by") or "heuristic",
            "steps": [s.get("op") for s in (plan.get("steps") or []) if isinstance(s, dict)],
            "note": "the run itself may replace this with an LLM plan when the policy asks for one",
        },
        "trust": trust_status(),
        "then": "harness → curator → Trust-gated approve of research drafts, in the background",
    }


def run_overrides(policy: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Fold this run's choices into a copy of the objective's policy.

    The Owner's picks in the console apply to the run they pressed, not to the
    objective for ever. Nothing here is written back: the caller passes the
    result as the run's own policy snapshot, and tomorrow's scheduled run reads
    the stored policy unchanged.
    """
    merged = dict(policy or {})
    if not overrides:
        return merged
    models = overrides.get("judge_models")
    if isinstance(models, list) and models:
        merged["judge_models"] = [str(m).strip() for m in models if str(m).strip()]
    triage = dict(merged.get("triage") or {})
    top_n = overrides.get("deep_judge_top_n")
    if isinstance(top_n, int) and top_n >= 0:
        triage["deep_judge_top_n"] = top_n
    symbols = overrides.get("symbols")
    if isinstance(symbols, list) and symbols:
        triage["symbols"] = [str(x).strip().upper() for x in symbols if str(x).strip()]
    if triage:
        merged["triage"] = triage
    return merged


def start_async_batch(
    conn: Any,
    obj: dict[str, Any],
    *,
    curate_after: bool,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the run row now and finish it in a background thread.

    Shared by ``POST /objectives/{id}/batch-run`` and the MCP write tool
    ``research.loop.run_objective``: the row exists before the caller returns,
    so Pipeline can poll live progress; harness → curate → Trust-L0 approve
    then run on their own connection. D10 BLOCKED — research drafts only.
    """
    import threading

    from bifrost_research.copilot.harness.runtime import _heuristic_plan
    from bifrost_research.db.conn import connect as db_connect

    objective_id = str(obj["id"])
    # A placeholder so the row exists before this call returns and the Pipeline
    # drawer can poll. Marked provisional: the background thread plans properly
    # — including the LLM chain when the policy asks for it — and replaces this.
    # Without the mark the runtime kept the placeholder, so every run started
    # from the UI planned heuristically while the unattended CronJob did not.
    plan = _heuristic_plan(obj)
    plan["generated_by"] = plan.get("generated_by") or "heuristic"
    plan["async_batch_start"] = True
    plan["provisional"] = True
    run = obj_repo.create_run(conn, objective_id=objective_id, plan_json=plan)
    run_id = str(run["id"])
    try:
        obj_repo.patch_run_trace(
            conn,
            run_id,
            {
                "events": [{"step": "queued", "label": "Queued", "decision": "async_batch_started"}],
                "progress": {"step": "queued", "label": "Queued", "detail": "Harness starting…"},
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("initial progress flush failed: %s", exc)
    trust = trust_status()
    obj_snapshot = dict(obj)
    obj_snapshot["policy_json"] = run_overrides(obj.get("policy_json") or {}, overrides)

    def _bg() -> None:
        bg_conn = None
        try:
            bg_conn = db_connect()
            existing = obj_repo.get_run(bg_conn, run_id)
            if existing is None:
                logger.error("batch-run bg: run %s missing", run_id)
                return
            process_objective(
                bg_conn,
                obj_snapshot,
                curate_after=curate_after,
                batch_mode=True,
                existing_run=existing,
            )
        except Exception:
            logger.exception("batch-run background failed for %s", run_id)
            if bg_conn is not None:
                try:
                    obj_repo.finish_run(
                        bg_conn,
                        run_id,
                        status="failed",
                        trace_json={
                            "events": [{"step": "failed", "decision": "background_error"}],
                            "progress": {
                                "step": "failed",
                                "label": "Failed",
                                "detail": "background batch-run error",
                            },
                        },
                        outputs={},
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("batch-run bg finish_run failed")
        finally:
            if bg_conn is not None:
                try:
                    bg_conn.close()
                except Exception:  # noqa: BLE001
                    pass

    threading.Thread(target=_bg, name=f"batch-run-{run_id}", daemon=True).start()
    return {
        "run": run,
        "started": True,
        "trust": trust,
        "advisory": (
            "D10 BLOCKED — batch started; Pipeline can poll live progress. "
            "Auto-approve is research drafts only."
        ),
    }


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
            try:
                knob = float(policy.get("min_source_hit_rate", DEFAULT_MIN_SOURCE_HIT_RATE))
            except (TypeError, ValueError):
                knob = DEFAULT_MIN_SOURCE_HIT_RATE
            approve_result = approve_all_for_run(
                conn,
                run_id,
                approved_by="system:loop_batch",
                owner_id=str(obj.get("owner_id") or "owner"),
                kinds_whitelist=RESEARCH_AUTO_APPROVE_KINDS,
                auto_validate=auto_validate,
                min_source_hit_rate=knob,
            )
            result["approve_all"] = approve_result
            held = int(approve_result.get("held_count") or 0)
            approved_n = int(approve_result.get("count") or 0)
            accepted = list(approve_result.get("accepted_symbols") or [])
            held_names = [str(h.get("symbol") or "") for h in (approve_result.get("held_symbols") or [])]
            step = "held" if not approved_n and not accepted else "approve_all"
            _append_batch_event(
                conn,
                run_id,
                step,
                label="Auto-accept",
                detail=(
                    f"accepted={len(accepted)} ({', '.join(accepted) or '—'}) "
                    f"held={len(held_names)} ({', '.join(held_names) or '—'}) drafts_approved={approved_n}"
                ),
                decision=f"accepted_{len(accepted)}_held_{len(held_names)}",
            )
            try:
                obj_repo.patch_run_outputs(
                    conn,
                    run_id,
                    {
                        "approve_all": {
                            "count": approved_n,
                            "held_count": held,
                            "accepted": accepted,
                            "held_symbols": approve_result.get("held_symbols") or [],
                            "partial": approve_result.get("partial") or [],
                            "leash": approve_result.get("leash"),
                            "skipped_batch": False,
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
