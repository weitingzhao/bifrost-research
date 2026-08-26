"""Morning / EOD agents + draft inbox API — Wave RS-E3.

Routes:
    POST /research/agents/morning/run
    POST /research/agents/eod/run
    GET  /research/drafts
    POST /research/drafts/{id}/approve
    POST /research/drafts/{id}/dismiss

D-RS-E-e: agents only create drafts; approve applies hypothesis mutations.
D10: no trading paths.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.auth.deps import require_owner
from bifrost_research.db.conn import connect
from bifrost_research.repositories import ai_action_log as action_repo
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import hypothesis as hyp_repo

logger = logging.getLogger(__name__)

agents_router = APIRouter(prefix="/research/agents", tags=["research-agents"])
drafts_router = APIRouter(prefix="/research/drafts", tags=["research-drafts"])


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _err(msg: str, status: int = 400) -> None:
    raise HTTPException(status_code=status, detail=msg)


class RunBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dry_run: bool = False


@agents_router.post("/morning/run")
def run_morning(body: RunBody | None = None) -> dict[str, Any]:
    from bifrost_research.copilot.agents.morning_prep import run_morning_prep

    dry = bool(body.dry_run) if body else False
    if dry or os.environ.get("BIFROST_MORNING_AGENT_DRY_RUN", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
    ):
        result = run_morning_prep(dry_run=True)
        return _ok(result)
    try:
        result = run_morning_prep(dry_run=False)
    except Exception as exc:  # noqa: BLE001
        logger.exception("morning agent failed")
        _err(f"morning agent failed: {exc}", 500)
    return _ok(result)


@agents_router.post("/eod/run")
def run_eod(body: RunBody | None = None) -> dict[str, Any]:
    from bifrost_research.copilot.agents.eod_review import run_eod_review

    dry = bool(body.dry_run) if body else False
    if dry or os.environ.get("BIFROST_EOD_AGENT_DRY_RUN", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
    ):
        result = run_eod_review(dry_run=True)
        return _ok(result)
    try:
        result = run_eod_review(dry_run=False)
    except Exception as exc:  # noqa: BLE001
        logger.exception("eod agent failed")
        _err(f"eod agent failed: {exc}", 500)
    return _ok(result)


@drafts_router.get("")
def list_drafts(
    status: str | None = Query(default="pending"),
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    conn = connect()
    try:
        rows = draft_repo.list_drafts(
            conn, status=status, kind=kind, limit=limit, offset=offset
        )
        pending = draft_repo.count_pending(conn)
        return _ok(
            {
                "rows": rows,
                "count": len(rows),
                "pending_count": pending,
                "limit": limit,
                "offset": offset,
            }
        )
    except ValueError as exc:
        _err(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("list drafts failed")
        _err(f"list drafts failed: {exc}", 500)
    finally:
        conn.close()


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    approved_by: str = Field(default="owner")


@drafts_router.post("/{draft_id}/approve")
def approve_draft(
    draft_id: str,
    body: ApproveBody | None = None,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    approved_by = (body.approved_by if body else owner_id) or owner_id
    conn = connect()
    try:
        draft = draft_repo.get_draft(conn, draft_id)
        if draft is None:
            _err("draft not found", 404)
        if draft["status"] != "pending":
            _err(f"draft status is {draft['status']}, expected pending", 409)

        payload = draft.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        executed: dict[str, Any] = {"draft_id": draft_id, "kind": draft["kind"]}
        hyp_result = None

        if draft["kind"] == "eod_verdict":
            hyp_id = payload.get("hypothesis_id") or draft.get("scope")
            proposed = payload.get("proposed_status")
            conclusion = payload.get("rationale") or payload.get("markdown")
            if hyp_id and proposed in {"active", "validated", "rejected", "archived"}:
                fields: dict[str, Any] = {"status": proposed}
                if conclusion:
                    fields["conclusion"] = str(conclusion)[:4000]
                hyp_result = hyp_repo.patch_hypothesis(conn, str(hyp_id), fields)
                executed["hypothesis"] = hyp_result
            else:
                executed["hypothesis"] = None
                executed["note"] = "no status applied (missing proposed_status or id)"

        elif draft["kind"] == "morning_brief":
            if payload.get("create_hypothesis") is True:
                title = str(payload.get("title") or "").strip()
                thesis = str(payload.get("thesis") or "").strip()
                if title and thesis:
                    hyp_result = hyp_repo.create_hypothesis(
                        conn,
                        title=title,
                        thesis=thesis,
                        symbols=payload.get("symbols") or [],
                        tags=list(payload.get("tags") or []) + ["morning_brief"],
                        origin_page="cockpit_inbox",
                        origin_ref={"draft_id": draft_id},
                    )
                    executed["hypothesis"] = hyp_result
                else:
                    executed["note"] = "create_hypothesis true but title/thesis missing"
            else:
                executed["note"] = "morning_brief approved (note only; no hypothesis write)"

        elif draft["kind"] == "playbook_rule":
            from bifrost_research.repositories import agent_persona as persona_repo
            from bifrost_research.repositories import playbook as playbook_repo

            agent_owner = str(payload.get("agent_owner") or "shared")
            rule = playbook_repo.create_rule(
                conn,
                owner_id=owner_id,
                title=str(payload.get("title") or "Untitled rule"),
                category=str(payload.get("category") or "general"),
                body_md=str(payload.get("body_md") or ""),
                trigger_ctx=payload.get("trigger_ctx") if isinstance(payload.get("trigger_ctx"), dict) else {},
                tags=list(payload.get("tags") or []),
                source_session_id=payload.get("source_session_id"),
                agent_owner=agent_owner,
            )
            executed["playbook_rule"] = rule
            persona_diff = payload.get("persona_diff")
            if isinstance(persona_diff, dict) and persona_diff and agent_owner != "shared":
                persona_row = persona_repo.apply_preference_diff(
                    conn,
                    owner_id,
                    agent_owner,
                    persona_diff,
                )
                executed["persona"] = persona_row

        elif draft["kind"] == "playbook_note":
            from bifrost_research.repositories import playbook as playbook_repo

            note = playbook_repo.create_note(
                conn,
                owner_id=owner_id,
                note_md=str(payload.get("note_md") or ""),
                tags=list(payload.get("tags") or []),
                symbols=list(payload.get("symbols") or []),
                source_session_id=payload.get("source_session_id"),
            )
            executed["playbook_note"] = note

        updated = draft_repo.update_draft_status(conn, draft_id, status="approved")
        linked = draft.get("linked_action_id")
        action_row = None
        if linked:
            action_row = action_repo.update_action_status(
                conn,
                linked,
                status="executed",
                approved_by=approved_by,
                executed_result=executed,
            )
        else:
            action_row = action_repo.insert_action(
                conn,
                action_kind="draft_approve",
                action_source="user_chat",
                input_payload={"draft_id": draft_id},
                output_payload=executed,
                status="executed",
            )
            action_repo.update_action_status(
                conn,
                action_row["id"],
                status="executed",
                approved_by=approved_by,
                executed_result=executed,
            )

        return _ok(
            {
                "draft": updated,
                "action": action_row,
                "executed": executed,
            }
        )
    except HTTPException:
        raise
    except ValueError as exc:
        _err(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("approve draft failed")
        _err(f"approve failed: {exc}", 500)
    finally:
        conn.close()


@drafts_router.post("/{draft_id}/dismiss")
def dismiss_draft(draft_id: str, body: ApproveBody | None = None) -> dict[str, Any]:
    approved_by = (body.approved_by if body else "owner") or "owner"
    conn = connect()
    try:
        draft = draft_repo.get_draft(conn, draft_id)
        if draft is None:
            _err("draft not found", 404)
        if draft["status"] != "pending":
            _err(f"draft status is {draft['status']}, expected pending", 409)

        updated = draft_repo.update_draft_status(conn, draft_id, status="dismissed")
        linked = draft.get("linked_action_id")
        action_row = None
        if linked:
            action_row = action_repo.update_action_status(
                conn,
                linked,
                status="rejected",
                approved_by=approved_by,
                executed_result={"draft_id": draft_id, "dismissed": True},
            )
        else:
            action_row = action_repo.insert_action(
                conn,
                action_kind="draft_dismiss",
                action_source="user_chat",
                input_payload={"draft_id": draft_id},
                output_payload={"dismissed": True},
                status="rejected",
            )

        return _ok({"draft": updated, "action": action_row})
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("dismiss draft failed")
        _err(f"dismiss failed: {exc}", 500)
    finally:
        conn.close()
