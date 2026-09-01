"""Harness objectives + runs API — Wave A (Stage 3 Agent Harness).

Routes:
    GET  /research/objectives
    POST /research/objectives
    POST /research/objectives/{id}/run
    GET  /research/objective-runs
    POST /research/objective-runs/{id}/curate
    POST /research/objective-runs/{id}/approve-all

D-Research-Harness: write research.* only; D10 BLOCKED — no trade execution.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.db.conn import connect
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


class ObjectiveCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    schedule: str = Field(default="adhoc")
    policy_json: dict[str, Any] = Field(default_factory=dict)
    persona: str = Field(default="loop_curator")
    owner_id: str = Field(default="owner")


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
