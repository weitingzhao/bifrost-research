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
    "pinned",
)

_SELECT_COLS = (
    "id, owner_id, title, model, agent_trail, messages, "
    "hypothesis_id, created_at, updated_at, expires_at, status, "
    "COALESCE(pinned, false) AS pinned"
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
            RETURNING {_SELECT_COLS}
            """,
            (sid, owner_id, title, model, json.dumps(msgs)),
        )
        row = cur.fetchone()
    conn.commit()
    return _serialize(_row(row, _COLUMNS))


def _is_valid_uuid(value: str | None) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def ensure_session(
    conn: _Connection,
    *,
    session_id: str | None,
    model: str,
    owner_id: str = "owner",
) -> str:
    """Return a valid session UUID, creating the row when missing or when id is invalid."""
    sid = session_id if _is_valid_uuid(session_id) else None
    if sid:
        existing = get_session(conn, sid)
        if existing is not None:
            return sid
        created = create_session(
            conn,
            model=model,
            owner_id=owner_id,
            session_id=sid,
        )
        return str(created["id"])
    new_id = str(uuid.uuid4())
    create_session(conn, model=model, owner_id=owner_id, session_id=new_id)
    return new_id


def append_turn(
    conn: _Connection,
    session_id: str,
    frames: list[dict[str, Any]],
    *,
    title: str | None = None,
) -> dict[str, Any] | None:
    """Append a full turn (user + tools + handoffs + assistant) atomically."""
    if not frames:
        return get_session(conn, session_id)

    agent_trail: list[dict[str, Any]] = []
    for frame in frames:
        if frame.get("kind") == "handoff":
            agent_trail.append(
                {
                    "from": frame.get("agent_from"),
                    "to": frame.get("agent_to"),
                    "at": frame.get("ts"),
                }
            )

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_RESEARCH_COPILOT_SESSION}
            SET messages = messages || %s::jsonb,
                agent_trail = COALESCE(agent_trail, '[]'::jsonb) || %s::jsonb,
                title = CASE WHEN %s::text IS NOT NULL AND length(trim(%s::text)) > 0
                             THEN COALESCE(title, %s::text) ELSE title END,
                expires_at = now() + interval '1 year',
                updated_at = now()
            WHERE id = %s::uuid AND status = 'active'
            RETURNING {_SELECT_COLS}
            """,
            (
                json.dumps(frames),
                json.dumps(agent_trail),
                title,
                title,
                title,
                session_id,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    return _serialize(_row(row, _COLUMNS))


def touch_expires_at(conn: _Connection, session_id: str) -> None:
    """Sliding retention — extend expiry when session is read if within 30 days of lapse."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_RESEARCH_COPILOT_SESSION}
            SET expires_at = now() + interval '1 year'
            WHERE id = %s::uuid
              AND status = 'active'
              AND expires_at < now() + interval '30 days'
            """,
            (session_id,),
        )
    conn.commit()


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
                RETURNING {_SELECT_COLS}
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
                RETURNING {_SELECT_COLS}
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


def get_session(
    conn: _Connection,
    session_id: str,
    owner_id: str | None = None,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        if owner_id:
            cur.execute(
                f"""
                SELECT {_SELECT_COLS}
                FROM {TABLE_RESEARCH_COPILOT_SESSION}
                WHERE id = %s::uuid AND owner_id = %s
                """,
                (session_id, owner_id),
            )
        else:
            cur.execute(
                f"""
                SELECT {_SELECT_COLS}
                FROM {TABLE_RESEARCH_COPILOT_SESSION}
                WHERE id = %s::uuid
                """,
                (session_id,),
            )
        row = cur.fetchone()
    if not row:
        return None
    touch_expires_at(conn, session_id)
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
            SELECT {_SELECT_COLS}
            FROM {TABLE_RESEARCH_COPILOT_SESSION}
            WHERE owner_id = %s AND status = 'active'
            ORDER BY pinned DESC, updated_at DESC
            LIMIT %s
            """,
            (owner_id, limit),
        )
        rows = cur.fetchall()
    return [_serialize(_row(r, _COLUMNS)) for r in rows]


def archive_session(
    conn: _Connection,
    session_id: str,
    owner_id: str | None = None,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        if owner_id:
            cur.execute(
                f"""
                UPDATE {TABLE_RESEARCH_COPILOT_SESSION}
                SET status = 'archived', updated_at = now()
                WHERE id = %s::uuid AND owner_id = %s
                RETURNING {_SELECT_COLS}
                """,
                (session_id, owner_id),
            )
        else:
            cur.execute(
                f"""
                UPDATE {TABLE_RESEARCH_COPILOT_SESSION}
                SET status = 'archived', updated_at = now()
                WHERE id = %s::uuid
                RETURNING {_SELECT_COLS}
                """,
                (session_id,),
            )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    return _serialize(_row(row, _COLUMNS))


def update_metadata(
    conn: _Connection,
    session_id: str,
    *,
    title: str | None = None,
    pinned: bool | None = None,
    owner_id: str | None = None,
) -> dict[str, Any] | None:
    """Update ``title`` and/or ``pinned`` for a session (best-effort, no-op if none).

    Callers pass ``None`` to leave a field unchanged.  Empty title is treated as
    "keep current title" — we never persist ``''`` because the FE would render
    a blank row.
    """
    if title is None and pinned is None:
        return get_session(conn, session_id, owner_id=owner_id)
    owner_clause = " AND owner_id = %s" if owner_id else ""
    params_tail: list[Any] = [title, title, title, pinned, session_id]
    if owner_id:
        params_tail.append(owner_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_RESEARCH_COPILOT_SESSION}
            SET title = CASE WHEN %s::text IS NOT NULL AND length(trim(%s::text)) > 0
                             THEN %s::text ELSE title END,
                pinned = COALESCE(%s::boolean, pinned),
                updated_at = now()
            WHERE id = %s::uuid AND status = 'active'{owner_clause}
            RETURNING {_SELECT_COLS}
            """,
            tuple(params_tail),
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
    "append_turn",
    "archive_session",
    "create_session",
    "derive_title",
    "ensure_session",
    "get_session",
    "list_recent",
    "touch_expires_at",
    "update_metadata",
]
