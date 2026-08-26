"""Copilot session HTTP routes — Wave RS-F4.3 (rename/pin — RS-UX5)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.auth.deps import require_owner
from bifrost_research.copilot.bridge_runtime import build_bridge
from bifrost_research.db.conn import connect
from bifrost_research.repositories import copilot_session as session_repo

router = APIRouter(prefix="/research/copilot/sessions", tags=["research-copilot-sessions"])


class SessionListResponse(BaseModel):
    rows: list[dict[str, Any]]


class SessionPatchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str | None = Field(default=None, max_length=120)
    pinned: bool | None = None
    group_name: str | None = Field(default=None, max_length=64)
    clear_group: bool = False


class BridgeBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    focus: str = Field(default="portfolio_risk")
    depth: str = Field(default="standard")
    target: str = Field(default="deepseek")
    model: str | None = Field(default="deepseek-chat")
    frames_from_message_id: str | None = None


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row.get("title"),
        "model": row.get("model"),
        "updated_at": row.get("updated_at"),
        "message_count": len(row.get("messages") or []),
        "pinned": bool(row.get("pinned") or False),
        "group_name": row.get("group_name"),
    }


@router.get("")
def list_sessions(
    limit: int = 20,
    owner_id: str = Depends(require_owner),
) -> SessionListResponse:
    limit = max(1, min(limit, 50))
    conn = connect()
    try:
        rows = session_repo.list_recent(conn, owner_id=owner_id, limit=limit)
    finally:
        conn.close()
    return SessionListResponse(rows=[_summary(r) for r in rows])


@router.get("/{session_id}")
def get_session(
    session_id: str,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    conn = connect()
    try:
        row = session_repo.get_session(conn, session_id, owner_id=owner_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session": row, "messages": row.get("messages") or []}


@router.patch("/{session_id}")
def patch_session(
    session_id: str,
    body: SessionPatchBody,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    if (
        body.title is None
        and body.pinned is None
        and body.group_name is None
        and not body.clear_group
    ):
        raise HTTPException(status_code=400, detail="nothing to update")
    conn = connect()
    try:
        row = session_repo.update_metadata(
            conn,
            session_id,
            title=body.title,
            pinned=body.pinned,
            group_name=body.group_name,
            clear_group=body.clear_group,
            owner_id=owner_id,
        )
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session": _summary(row)}


@router.delete("/{session_id}")
def archive_session(
    session_id: str,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    conn = connect()
    try:
        row = session_repo.archive_session(conn, session_id, owner_id=owner_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"archived": True, "session": _summary(row)}


@router.post("/{session_id}/bridge")
def bridge_session(
    session_id: str,
    body: BridgeBody,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    result = build_bridge(
        session_id=session_id,
        owner_id=owner_id,
        focus=body.focus,
        depth=body.depth,
        target=body.target,
        model=body.model,
        frames_from_message_id=body.frames_from_message_id,
    )
    if not result.get("ok"):
        err = result.get("error")
        if err == "bridge_rate_limit":
            raise HTTPException(
                status_code=429,
                detail={
                    "error": err,
                    "retry_after_sec": result.get("retry_after_sec"),
                    "limit_per_minute": result.get("limit_per_minute"),
                },
            )
        if err == "session_not_found":
            raise HTTPException(status_code=404, detail=err)
        if err == "empty_context":
            raise HTTPException(status_code=400, detail=err)
        raise HTTPException(status_code=400, detail=str(err))
    return result


__all__ = ["router"]
