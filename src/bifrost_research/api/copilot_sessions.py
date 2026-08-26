"""Copilot session HTTP routes — Wave RS-F4.3."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.db.conn import connect
from bifrost_research.repositories import copilot_session as session_repo

router = APIRouter(prefix="/research/copilot/sessions", tags=["research-copilot-sessions"])


class SessionListResponse(BaseModel):
    rows: list[dict[str, Any]]


@router.get("")
def list_sessions(limit: int = 20) -> SessionListResponse:
    limit = max(1, min(limit, 50))
    conn = connect()
    try:
        rows = session_repo.list_recent(conn, limit=limit)
    finally:
        conn.close()
    summaries = [
        {
            "id": r["id"],
            "title": r.get("title"),
            "model": r.get("model"),
            "updated_at": r.get("updated_at"),
            "message_count": len(r.get("messages") or []),
        }
        for r in rows
    ]
    return SessionListResponse(rows=summaries)


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


@router.delete("/{session_id}")
def archive_session(session_id: str) -> dict[str, Any]:
    conn = connect()
    try:
        row = session_repo.archive_session(conn, session_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"archived": True, "session": row}


__all__ = ["router"]
