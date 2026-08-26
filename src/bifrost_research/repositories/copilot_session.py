"""SQL layer for ``research.copilot_session`` — Wave RS-F4.2."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Protocol

from bifrost_research.schema.schemas import TABLE_RESEARCH_COPILOT_SESSION


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...


def _row(row: Any, cols: tuple[str, ...]) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return {cols[i]: row[i] for i in range(min(len(cols), len(row)))}


_COLUMNS = (
    "id",
    "owner_id",
    "title",
    "model",
    "agent_trail",
    "messages",
    "hypothesis_id",
    "created_at",
    "updated_at",
    "expires_at",
    "status",
)


def create_session(
    conn: _Connection,
    *,
    model: str,
    owner_id: str = "owner",
    title: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    sid = session_id or str(uuid.uuid4())
    msgs = messages or []
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_RESEARCH_COPILOT_SESSION}
                (id, owner_id, title, model, messages)
            VALUES (%s::uuid, %s, %s, %s, %s::jsonb)
            RETURNING id, owner_id, title, model, agent_trail, messages,
                      hypothesis_id, created_at, updated_at, expires_at, status
            """,
            (sid, owner_id, title, model, json.dumps(msgs)),
        )
        row = cur.fetchone()
    conn.commit()
    return _serialize(_row(row, _COLUMNS))


def append_message(
    conn: _Connection,
    session_id: str,
    message: dict[str, Any],
    *,
    agent_trail: list[dict[str, Any]] | None = None,
    title: str | None = None,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        if title:
            cur.execute(
                f"""
                UPDATE {TABLE_RESEARCH_COPILOT_SESSION}
                SET messages = messages || %s::jsonb,
                    agent_trail = COALESCE(%s::jsonb, agent_trail),
                    title = COALESCE(title, %s),
                    updated_at = now()
                WHERE id = %s::uuid AND status = 'active'
                RETURNING id, owner_id, title, model, agent_trail, messages,
                          hypothesis_id, created_at, updated_at, expires_at, status
                """,
                (
                    json.dumps([message]),
                    json.dumps(agent_trail) if agent_trail is not None else None,
                    title,
                    session_id,
                ),
            )
        else:
            cur.execute(
                f"""
                UPDATE {TABLE_RESEARCH_COPILOT_SESSION}
                SET messages = messages || %s::jsonb,
                    agent_trail = COALESCE(%s::jsonb, agent_trail),
                    updated_at = now()
                WHERE id = %s::uuid AND status = 'active'
                RETURNING id, owner_id, title, model, agent_trail, messages,
                          hypothesis_id, created_at, updated_at, expires_at, status
                """,
                (
                    json.dumps([message]),
                    json.dumps(agent_trail) if agent_trail is not None else None,
                    session_id,
                ),
            )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    return _serialize(_row(row, _COLUMNS))


def get_session(conn: _Connection, session_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, owner_id, title, model, agent_trail, messages,
                   hypothesis_id, created_at, updated_at, expires_at, status
            FROM {TABLE_RESEARCH_COPILOT_SESSION}
            WHERE id = %s::uuid
            """,
            (session_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return _serialize(_row(row, _COLUMNS))


def list_recent(
    conn: _Connection,
    *,
    owner_id: str = "owner",
    limit: int = 20,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, owner_id, title, model, agent_trail, messages,
                   hypothesis_id, created_at, updated_at, expires_at, status
            FROM {TABLE_RESEARCH_COPILOT_SESSION}
            WHERE owner_id = %s AND status = 'active'
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (owner_id, limit),
        )
        rows = cur.fetchall()
    return [_serialize(_row(r, _COLUMNS)) for r in rows]


def archive_session(conn: _Connection, session_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_RESEARCH_COPILOT_SESSION}
            SET status = 'archived', updated_at = now()
            WHERE id = %s::uuid
            RETURNING id, owner_id, title, model, agent_trail, messages,
                      hypothesis_id, created_at, updated_at, expires_at, status
            """,
            (session_id,),
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    return _serialize(_row(row, _COLUMNS))


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if "id" in out and out["id"] is not None:
        out["id"] = str(out["id"])
    for col in ("agent_trail", "messages"):
        val = out.get(col)
        if isinstance(val, str):
            out[col] = json.loads(val)
    for col in ("created_at", "updated_at", "expires_at"):
        val = out.get(col)
        if isinstance(val, datetime):
            out[col] = val.isoformat()
    return out


def derive_title(first_user_message: str, *, max_len: int = 60) -> str:
    text = (first_user_message or "").strip().replace("\n", " ")
    if len(text) <= max_len:
        return text or "New session"
    return text[: max_len - 1] + "…"


__all__ = [
    "append_message",
    "archive_session",
    "create_session",
    "derive_title",
    "get_session",
    "list_recent",
]
