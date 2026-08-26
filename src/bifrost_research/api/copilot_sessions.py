"""Copilot session HTTP routes — Wave RS-F4.3 (rename/pin — RS-UX5)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.db.conn import connect
from bifrost_research.repositories import copilot_session as session_repo

router = APIRouter(prefix="/research/copilot/sessions", tags=["research-copilot-sessions"])


class SessionListResponse(BaseModel):
    rows: list[dict[str, Any]]


class SessionPatchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str | None = Field(default=None, max_length=120)
    pinned: bool | None = None


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row.get("title"),
        "model": row.get("model"),
        "updated_at": row.get("updated_at"),
        "message_count": len(row.get("messages") or []),
        "pinned": bool(row.get("pinned") or False),
    }


@router.get("")
def list_sessions(limit: int = 20) -> SessionListResponse:
    limit = max(1, min(limit, 50))
    conn = connect()
    try:
        rows = session_repo.list_recent(conn, limit=limit)
    finally:
        conn.close()
    return SessionListResponse(rows=[_summary(r) for r in rows])


@router.get("/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    conn = connect()
    try:
        row = session_repo.get_session(conn, session_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session": row, "messages": row.get("messages") or []}


@router.patch("/{session_id}")
def patch_session(session_id: str, body: SessionPatchBody) -> dict[str, Any]:
    if body.title is None and body.pinned is None:
        raise HTTPException(status_code=400, detail="nothing to update")
    conn = connect()
    try:
        row = session_repo.update_metadata(
            conn,
            session_id,
            title=body.title,
            pinned=body.pinned,
        )
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session": _summary(row)}


@router.delete("/{session_id}")
def archive_session(session_id: str) -> dict[str, Any]:
    conn = connect()
    try:
        row = session_repo.archive_session(conn, session_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"archived": True, "session": _summary(row)}


__all__ = ["router"]
