"""Harness objectives + runs API — Wave A (Stage 3 Agent Harness).

Routes:
    GET  /research/objectives
    POST /research/objectives
    POST /research/objectives/{id}/run
    POST /research/objectives/{id}/batch-run
    GET  /research/objective-runs
    DELETE /research/objective-runs/{id}?force=
    POST /research/objective-runs/{id}/curate
    POST /research/objective-runs/{id}/approve-all
    GET  /research/loop/trust

D-Research-Harness: write research.* only; D10 BLOCKED — no trade execution.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.auth.deps import require_owner
from bifrost_research.db.conn import connect
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import objective as obj_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research", tags=["research-harness"])


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


class ObjectiveStatusPatch(BaseModel):
    """`archived` retires an objective; `active` brings it back."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(..., min_length=1, max_length=32)


class ObjectiveCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    schedule: str = Field(default="adhoc")
    policy_json: dict[str, Any] = Field(default_factory=dict)
    persona: str = Field(default="loop_curator")
    owner_id: str = Field(default="owner")


class BatchRunBody(BaseModel):
    """UI / HTTP equivalent of CLI ``--batch-mode`` (+ optional curate)."""

    model_config = ConfigDict(extra="forbid")

    curate_after: bool = True


@router.get("/objectives")
def list_objectives(
    status: str | None = Query(default="active"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        rows = obj_repo.list_objectives(conn, status=status, limit=limit)
    finally:
        conn.close()
    return _ok({"items": rows, "count": len(rows)})


@router.post("/objectives")
def create_objective(body: ObjectiveCreate) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        try:
            row = obj_repo.create_objective(
                conn,
                title=body.title,
                description=body.description,
                schedule=body.schedule,
                policy_json=body.policy_json,
                persona=body.persona,
                owner_id=body.owner_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
    return _ok(row)


@router.patch("/objectives/{objective_id}")
def set_objective_status(objective_id: str, body: ObjectiveStatusPatch) -> dict[str, Any]:
    """Archive an objective, or bring an archived one back.

    Archiving is the retirement path: the console lists active objectives, so
    this removes it from view while its runs, funnels and candidate lineage stay
    exactly where they are.
    """
    conn = _connect_or_503()
    try:
        row = obj_repo.set_objective_status(conn, objective_id, status=body.status)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="objective not found")
    return _ok(row)


@router.delete("/objectives/{objective_id}")
def delete_objective(objective_id: str) -> dict[str, Any]:
    """Delete an objective that never ran.

    Refused once it has runs — deleting it would take the run history and the
    candidate lineage pointing at it. The foreign key would refuse anyway; this
    returns the reason and the run count instead of a 500 from an integrity
    error, and names archiving as the thing the caller probably wants.
    """
    conn = _connect_or_503()
    try:
        existing = obj_repo.get_objective(conn, objective_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="objective not found")
        runs = obj_repo.count_runs(conn, objective_id)
        if runs > 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"objective has {runs} run(s) — deleting it would take their "
                    "funnels and the candidates that reference them. Archive it "
                    "instead."
                ),
            )
        obj_repo.delete_objective(conn, objective_id)
    finally:
        conn.close()
    return _ok({"id": objective_id, "deleted": True})


@router.post("/objectives/{objective_id}/run")
def run_objective(objective_id: str) -> dict[str, Any]:
    """Trigger an objective run (synchronous lightweight harness for DEV)."""
    from bifrost_research.copilot.harness.runtime import run_objective as harness_run

    conn = _connect_or_503()
    try:
        obj = obj_repo.get_objective(conn, objective_id)
        if obj is None:
            raise HTTPException(status_code=404, detail="objective not found")
        try:
            result = harness_run(conn, objective=obj)
        except Exception as exc:
            logger.exception("harness run failed")
            raise HTTPException(status_code=500, detail=f"harness failed: {exc}") from exc
    finally:
        conn.close()
    return _ok(result)


@router.post("/objectives/{objective_id}/batch-run")
def batch_run_objective(
    objective_id: str,
    body: BatchRunBody | None = None,
) -> dict[str, Any]:
    """Start unattended batch and return the run id immediately.

    Creates a ``running`` row, returns ``{ run, started, trust }``, then finishes
    harness → curate → Trust-L0 approve in a background thread so Pipeline can
    poll mid-run progress. D10 BLOCKED — research drafts only.
    """
    import threading

    from bifrost_research.copilot.harness.batch_orchestrate import (
        process_objective,
        trust_status,
    )
    from bifrost_research.copilot.harness.runtime import _heuristic_plan
    from bifrost_research.db.conn import connect as db_connect

    payload = body or BatchRunBody()
    conn = _connect_or_503()
    try:
        obj = obj_repo.get_objective(conn, objective_id)
        if obj is None:
            raise HTTPException(status_code=404, detail="objective not found")
        if obj.get("status") != "active":
            raise HTTPException(
                status_code=409,
                detail=f"objective status {obj.get('status')!r} is not active",
            )
        # Fast create so FE can open Pipeline before the heavy work starts.
        plan = _heuristic_plan(obj)
        plan["generated_by"] = plan.get("generated_by") or "heuristic"
        plan["async_batch_start"] = True
        run = obj_repo.create_run(conn, objective_id=objective_id, plan_json=plan)
        run_id = str(run["id"])
        try:
            obj_repo.patch_run_trace(
                conn,
                run_id,
                {
                    "events": [
                        {
                            "step": "queued",
                            "label": "Queued",
                            "decision": "async_batch_started",
                        }
                    ],
                    "progress": {
                        "step": "queued",
                        "label": "Queued",
                        "detail": "Harness starting…",
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("initial progress flush failed: %s", exc)
        trust = trust_status()
        obj_snapshot = dict(obj)
        curate_after = payload.curate_after
    finally:
        conn.close()

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
    return _ok(
        {
            "run": run,
            "started": True,
            "trust": trust,
            "advisory": (
                "D10 BLOCKED — batch started; Pipeline can poll live progress. "
                "Auto-approve is research drafts only."
            ),
        }
    )


@router.get("/loop/trust")
def get_loop_trust() -> dict[str, Any]:
    """Trust gate observability for Harness Console (batch auto-approve)."""
    from bifrost_research.copilot.harness.batch_orchestrate import trust_status

    return _ok(trust_status())


@router.get("/objective-runs/{run_id}")
def get_objective_run(run_id: str) -> dict[str, Any]:
    """Single run detail for white-box pipeline UI (LS-3)."""
    conn = _connect_or_503()
    try:
        run = obj_repo.get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        obj = obj_repo.get_objective(conn, str(run.get("objective_id")))
        payload = dict(run)
        if obj:
            payload["objective_title"] = obj.get("title")
            payload["objective_policy_json"] = obj.get("policy_json")
    finally:
        conn.close()
    return _ok(payload)


@router.get("/objective-runs")
def list_objective_runs(
    status: str | None = Query(default=None),
    objective_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        rows = obj_repo.list_runs(
            conn,
            status=status,
            objective_id=objective_id,
            limit=limit,
        )
    finally:
        conn.close()
    return _ok({"items": rows, "count": len(rows)})


@router.delete("/objective-runs/{run_id}")
def delete_run(
    run_id: str,
    force: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Delete one run. Default refuses while candidates point at it.

    ``force=true`` cascades: deletes those candidates, dismisses pending drafts
    for the run, then deletes the run. Promoted hypotheses are kept.
    """
    conn = _connect_or_503()
    try:
        existing = obj_repo.get_run(conn, run_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="run not found")
        if force:
            try:
                result = obj_repo.force_delete_run(conn, run_id)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return _ok(result)

        linked = obj_repo.count_candidates_for_run(conn, run_id)
        if linked > 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{linked} candidate(s) still point at this run — deleting it "
                    "would leave their lineage dangling. Use force=true to remove "
                    "those candidates and dismiss pending drafts (hypotheses kept)."
                ),
            )
        obj_repo.delete_run(conn, run_id)
    finally:
        conn.close()
    return _ok({"id": run_id, "deleted": True, "force": False})


@router.post("/objective-runs/{run_id}/curate")
def curate_run(run_id: str) -> dict[str, Any]:
    """Headless CuratorRun for an objective run (LO-1)."""
    from bifrost_research.copilot.curator.runtime import run_curator_for_run

    conn = _connect_or_503()
    try:
        run = obj_repo.get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run.get("status") not in {"awaiting_approval", "running", "completed"}:
            raise HTTPException(
                status_code=409,
                detail=f"run status {run.get('status')!r} not eligible for curate",
            )
        try:
            result = run_curator_for_run(conn, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("curate failed")
            raise HTTPException(status_code=500, detail=f"curate failed: {exc}") from exc
    finally:
        conn.close()
    return _ok(result)


@router.post("/objective-runs/{run_id}/approve-all")
def approve_all_for_run_endpoint(run_id: str) -> dict[str, Any]:
    """Approve pending drafts for this run using Inbox ``apply_draft_approval``.

    Same side effects as Decision Inbox Approve (policy merge, candidate
    promote + hypotheses, action status). Per-draft HTTP errors are collected
    so one bad draft does not 500 the batch.
    """
    from bifrost_research.copilot.harness.batch import approve_all_for_run as batch_approve

    conn = _connect_or_503()
    try:
        run = obj_repo.get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        obj = obj_repo.get_objective(conn, str(run.get("objective_id")))
        policy = (obj or {}).get("policy_json") or {}
        auto_validate = bool(policy.get("auto_validate", False))
        result = batch_approve(
            conn,
            run_id,
            approved_by="owner",
            owner_id="owner",
            kinds_whitelist=None,
            auto_validate=auto_validate,
        )
    finally:
        conn.close()
    return _ok(result)


class PolicySuggestionBody(BaseModel):
    """An Owner-authored change to an objective's policy."""

    model_config = ConfigDict(extra="forbid")

    suggestion: dict[str, Any] = Field(
        ..., description="Policy fields to change, whitelist-checked before the draft is made."
    )
    rationale: str = Field(
        default="", description="Why. Stored with the change so drift can be attributed to it."
    )


@router.post("/objectives/{objective_id}/policy-suggestion")
def create_policy_suggestion(
    objective_id: str,
    body: PolicySuggestionBody,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    """Propose a policy change as a draft, the same way the model does.

    The Owner could not originate one: policy_suggestion is not a manually
    creatable kind, and PATCH /objectives only takes status. So the trading
    system was readable and not adjustable.

    Routed through a draft rather than written straight onto the objective so
    that every policy change — model-proposed or Owner-proposed — leaves the
    same record. Without one, rule drift is unreadable: `sepa −3,431 → −1,204`
    means the market moved *or* that someone lowered min_score, and those are
    opposite conclusions. The draft carries the rationale so the answer is on
    the record rather than in someone's memory.

    Keys are checked here rather than dropped at approval. patch_policy_json
    filters silently, which is how a suggestion that changes nothing used to
    reach the Inbox looking like one that does.
    """
    if not body.suggestion:
        raise HTTPException(status_code=400, detail="suggestion must not be empty")

    unknown = sorted(set(body.suggestion) - obj_repo.POLICY_SUGGESTION_WHITELIST)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"not applicable policy fields: {unknown}. Approving would drop them "
                f"silently. Allowed: {sorted(obj_repo.POLICY_SUGGESTION_WHITELIST)}"
            ),
        )

    conn = _connect_or_503()
    try:
        objective = obj_repo.get_objective(conn, objective_id)
        if objective is None:
            raise HTTPException(status_code=404, detail="objective not found")

        # Validated strictly, not with parse_policy — that one is fail-soft by
        # design so a stored policy can always be read, and it answers an
        # invalid value by logging and falling back to defaults. Accepting
        # `max_candidates: 9999` here would store a policy that says 9999 while
        # every run quietly does something else.
        from bifrost_research.copilot.harness.policy_schema import LoopPolicy

        merged = {**(objective.get("policy_json") or {}), **body.suggestion}
        try:
            LoopPolicy.model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail=f"policy would not be valid: {exc}"
            ) from exc

        draft = draft_repo.insert_draft(
            conn,
            kind="policy_suggestion",
            payload={
                "objective_id": objective_id,
                "suggestion": body.suggestion,
                "rationale": body.rationale.strip(),
                "objective_title": objective.get("title"),
                # Snapshot, not a live read: the Inbox diff should show what the
                # proposer was looking at. Without it every row rendered its
                # current value as "not set", so an 8 → 10 change read as though
                # nothing had been set before.
                "current_policy": objective.get("policy_json") or {},
                # Tells the Inbox this came from the Owner, not the model.
                "manual": True,
                "created_by": owner_id,
            },
            scope=f"objective:{objective_id}",
            generated_by=f"owner:{owner_id}",
        )
    finally:
        conn.close()
    return _ok({"draft": draft})
