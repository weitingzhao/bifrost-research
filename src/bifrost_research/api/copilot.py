"""Research Copilot HTTP routes — Wave RS-E2 / RS-E4.

POST /research/copilot/stream   → SSE (token | tool_call | tool_result | error | done)
GET  /research/copilot/usage    → daily tokens / cost / remaining cap
POST /research/copilot/approve  → issue short-lived HMAC approval token
POST /research/copilot/execute  → run write tool with approval_token (dry_run=false)
POST /research/copilot/dismiss  → mark proposed write as rejected in ai_action_log
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.copilot.approvals import (
    ApprovalError,
    issue_token,
    strip_meta_args,
)
from bifrost_research.copilot.orchestrator import execute_approved_write, orchestrate
from bifrost_research.copilot.rate_limit import check_rate_limit, get_usage, usage_to_dict
from bifrost_research.db.conn import connect
from bifrost_research.mcp.tools._write_common import WRITE_TOOL_NAMES
from bifrost_research.repositories import ai_action_log as action_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/copilot", tags=["research-copilot"])


class CopilotMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str = ""
    tool_call_id: str | None = None
    name: str | None = None


class CopilotStreamBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    messages: list[CopilotMessage] = Field(default_factory=list)
    model: str = Field(default="claude-4.5-sonnet")
    max_tools: int = Field(default=8, ge=0, le=32)
    session_id: str | None = None


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    action_id: str | None = None
    session_id: str | None = None
    approved_by: str = Field(default="owner")
    preview: dict[str, Any] | None = None


class ExecuteBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    approval_token: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    approved_by: str = Field(default="owner")
    action_id: str | None = None


class DismissBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    action_id: str | None = None
    session_id: str | None = None
    approved_by: str = Field(default="owner")
    reason: str | None = None


@router.get("/usage")
def copilot_usage() -> dict[str, Any]:
    return usage_to_dict(get_usage())


@router.post("/stream")
async def copilot_stream(body: CopilotStreamBody, request: Request) -> StreamingResponse:
    snap = check_rate_limit()
    if snap is None:
        raise HTTPException(
            status_code=429,
            detail="Daily AI cap reached — resets at 00:00 UTC",
            headers={"Retry-After": "3600"},
        )

    messages = [m.model_dump(exclude_none=True) for m in body.messages]
    if not messages:
        raise HTTPException(status_code=400, detail="messages required")

    provider = getattr(request.app.state, "copilot_provider", None)
    mcp = getattr(request.app.state, "copilot_mcp", None)

    async def event_gen():
        async for frame in orchestrate(
            messages=messages,
            model=body.model,
            max_tools=body.max_tools,
            session_id=body.session_id,
            provider=provider,
            mcp=mcp,
        ):
            if await request.is_disconnected():
                break
            yield frame

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/approve")
def copilot_approve(body: ApproveBody) -> dict[str, Any]:
    if body.tool_name not in WRITE_TOOL_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"tool_name must be a write tool; got {body.tool_name!r}",
        )
    args = strip_meta_args(body.arguments)
    action_id = (body.action_id or "").strip() or action_repo.generate_action_id()

    try:
        issued = issue_token(
            action_id=action_id,
            tool=body.tool_name,
            arguments=args,
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    # Best-effort audit: proposed → approved (token issued)
    action_row: dict[str, Any] | None = None
    try:
        conn = connect()
        try:
            existing = action_repo.get_action(conn, action_id)
            if existing is None:
                action_row = action_repo.insert_action(
                    conn,
                    action_id=action_id,
                    action_kind=body.tool_name,
                    action_source="user_chat",
                    input_payload={
                        "tool_name": body.tool_name,
                        "arguments": args,
                        "preview": body.preview,
                    },
                    status="approved",
                    session_id=body.session_id,
                )
                action_repo.update_action_status(
                    conn,
                    action_id,
                    status="approved",
                    approved_by=body.approved_by,
                )
                action_row = action_repo.get_action(conn, action_id)
            else:
                action_row = action_repo.update_action_status(
                    conn,
                    action_id,
                    status="approved",
                    approved_by=body.approved_by,
                )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — approve must still return token without DB
        logger.exception("ai_action_log approve write failed (token still issued)")

    return {
        "ok": True,
        "data": {
            **issued,
            "action": action_row,
            "arguments": args,
        },
    }


@router.post("/execute")
async def copilot_execute(body: ExecuteBody, request: Request) -> dict[str, Any]:
    if body.tool_name not in WRITE_TOOL_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"tool_name must be a write tool; got {body.tool_name!r}",
        )
    if not body.approval_token or not body.approval_token.strip():
        raise HTTPException(status_code=403, detail="approval token required")

    args = strip_meta_args(body.arguments)
    mcp = getattr(request.app.state, "copilot_mcp", None)

    result = await execute_approved_write(
        tool_name=body.tool_name,
        arguments=args,
        approval_token=body.approval_token.strip(),
        mcp=mcp,
    )

    status = result.get("status")
    if result.get("ok") is False and isinstance(status, int) and status >= 400:
        raise HTTPException(status_code=status, detail=result.get("error") or "execute failed")

    # Audit executed
    action_id = (body.action_id or "").strip()
    if not action_id:
        # recover from token prefix (action_id|tool|...)
        try:
            action_id = body.approval_token.split("|", 1)[0]
        except Exception:  # noqa: BLE001
            action_id = ""

    action_row: dict[str, Any] | None = None
    try:
        conn = connect()
        try:
            if action_id:
                existing = action_repo.get_action(conn, action_id)
                if existing is None:
                    action_repo.insert_action(
                        conn,
                        action_id=action_id,
                        action_kind=body.tool_name,
                        action_source="user_chat",
                        input_payload={
                            "tool_name": body.tool_name,
                            "arguments": args,
                        },
                        status="proposed",
                        session_id=body.session_id,
                    )
                action_row = action_repo.update_action_status(
                    conn,
                    action_id,
                    status="executed" if result.get("ok") else "error",
                    approved_by=body.approved_by,
                    executed_result=result,
                )
            else:
                action_row = action_repo.insert_action(
                    conn,
                    action_kind=body.tool_name,
                    action_source="user_chat",
                    input_payload={"tool_name": body.tool_name, "arguments": args},
                    output_payload=result,
                    status="executed" if result.get("ok") else "error",
                    session_id=body.session_id,
                )
                if action_row and result.get("ok"):
                    action_row = action_repo.update_action_status(
                        conn,
                        action_row["id"],
                        status="executed",
                        approved_by=body.approved_by,
                        executed_result=result,
                    )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.exception("ai_action_log execute write failed")

    return {"ok": bool(result.get("ok")), "data": {"result": result, "action": action_row}}


@router.post("/dismiss")
def copilot_dismiss(body: DismissBody) -> dict[str, Any]:
    """Reject a proposed write (FE dismiss) — optional telemetry into ai_action_log."""
    args = strip_meta_args(body.arguments)
    action_id = (body.action_id or "").strip() or action_repo.generate_action_id()
    action_row: dict[str, Any] | None = None
    try:
        conn = connect()
        try:
            existing = action_repo.get_action(conn, action_id) if body.action_id else None
            if existing is None:
                action_row = action_repo.insert_action(
                    conn,
                    action_id=action_id,
                    action_kind=body.tool_name or "copilot_write_dismiss",
                    action_source="user_chat",
                    input_payload={
                        "tool_name": body.tool_name,
                        "arguments": args,
                        "reason": body.reason,
                    },
                    status="rejected",
                    session_id=body.session_id,
                )
                action_row = action_repo.update_action_status(
                    conn,
                    action_id,
                    status="rejected",
                    approved_by=body.approved_by,
                    executed_result={"dismissed": True, "reason": body.reason},
                )
            else:
                action_row = action_repo.update_action_status(
                    conn,
                    action_id,
                    status="rejected",
                    approved_by=body.approved_by,
                    executed_result={"dismissed": True, "reason": body.reason},
                )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.exception("ai_action_log dismiss failed")
        # Still return ok for FE UX — dismiss is local-first
        return {
            "ok": True,
            "data": {
                "action_id": action_id,
                "status": "rejected",
                "persisted": False,
            },
        }
    return {"ok": True, "data": {"action": action_row, "status": "rejected"}}


__all__ = ["router"]
