"""SQL layer for playbook tables — RS-KB3."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Protocol

from bifrost_research.schema.schemas import (
    TABLE_RESEARCH_PLAYBOOK_CASE,
    TABLE_RESEARCH_PLAYBOOK_NOTE,
    TABLE_RESEARCH_PLAYBOOK_RULE,
)


class _Connection(Protocol):
    def cursor(self) -> Any: ...
    def commit(self) -> None: ...


def _row(row: Any, cols: tuple[str, ...]) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return {cols[i]: row[i] for i in range(min(len(cols), len(row)))}


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if out.get("id") is not None:
        out["id"] = str(out["id"])
    for col in ("trigger_ctx", "trade_ref", "source_msg_ref"):
        val = out.get(col)
        if isinstance(val, str):
            out[col] = json.loads(val)
    for col in ("created_at", "updated_at", "retired_at"):
        val = out.get(col)
        if isinstance(val, datetime):
            out[col] = val.isoformat()
    if out.get("related_rule_ids"):
        out["related_rule_ids"] = [str(x) for x in out["related_rule_ids"]]
    return out


_RULE_COLS = (
    "id",
    "owner_id",
    "title",
    "category",
    "body_md",
    "trigger_ctx",
    "tags",
    "active",
    "source_session_id",
    "source_msg_ref",
    "created_at",
    "updated_at",
    "retired_at",
)


def list_rules(
    conn: _Connection,
    *,
    owner_id: str,
    category: str | None = None,
    active_only: bool = True,
    symbol: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses = ["owner_id = %s"]
    params: list[Any] = [owner_id]
    if active_only:
        clauses.append("active = true")
        clauses.append("retired_at IS NULL")
    if category:
        clauses.append("category = %s")
        params.append(category)
    if symbol:
        clauses.append("(%s = ANY(tags) OR trigger_ctx->'symbols' ? %s)")
        params.extend([symbol, symbol])
    if tags:
        clauses.append("tags && %s::text[]")
        params.append(tags)
    params.append(limit)
    where = " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, owner_id, title, category, body_md, trigger_ctx, tags,
                   active, source_session_id, source_msg_ref,
                   created_at, updated_at, retired_at
            FROM {TABLE_RESEARCH_PLAYBOOK_RULE}
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = cur.fetchall()
    return [_serialize(_row(r, _RULE_COLS)) for r in rows]


def create_rule(
    conn: _Connection,
    *,
    owner_id: str,
    title: str,
    category: str,
    body_md: str,
    trigger_ctx: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    source_session_id: str | None = None,
    source_msg_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_RESEARCH_PLAYBOOK_RULE}
                (id, owner_id, title, category, body_md, trigger_ctx, tags,
                 source_session_id, source_msg_ref)
            VALUES (%s::uuid, %s, %s, %s, %s, %s::jsonb, %s::text[],
                    %s::uuid, %s::jsonb)
            RETURNING id, owner_id, title, category, body_md, trigger_ctx, tags,
                      active, source_session_id, source_msg_ref,
                      created_at, updated_at, retired_at
            """,
            (
                rid,
                owner_id,
                title,
                category,
                body_md,
                json.dumps(trigger_ctx or {}),
                tags or [],
                source_session_id,
                json.dumps(source_msg_ref) if source_msg_ref else None,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return _serialize(_row(row, _RULE_COLS))


def update_rule(
    conn: _Connection,
    rule_id: str,
    owner_id: str,
    fields: dict[str, Any],
) -> dict[str, Any] | None:
    allowed = {"title", "category", "body_md", "trigger_ctx", "tags", "active"}
    sets: list[str] = []
    params: list[Any] = []
    for key, val in fields.items():
        if key not in allowed:
            continue
        if key == "trigger_ctx":
            sets.append("trigger_ctx = %s::jsonb")
            params.append(json.dumps(val))
        elif key == "tags":
            sets.append("tags = %s::text[]")
            params.append(val)
        else:
            sets.append(f"{key} = %s")
            params.append(val)
    if not sets:
        return get_rule(conn, rule_id, owner_id)
    sets.append("updated_at = now()")
    params.extend([rule_id, owner_id])
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_RESEARCH_PLAYBOOK_RULE}
            SET {", ".join(sets)}
            WHERE id = %s::uuid AND owner_id = %s
            RETURNING id, owner_id, title, category, body_md, trigger_ctx, tags,
                      active, source_session_id, source_msg_ref,
                      created_at, updated_at, retired_at
            """,
            tuple(params),
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    return _serialize(_row(row, _RULE_COLS))


def retire_rule(conn: _Connection, rule_id: str, owner_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_RESEARCH_PLAYBOOK_RULE}
            SET active = false, retired_at = now(), updated_at = now()
            WHERE id = %s::uuid AND owner_id = %s
            RETURNING id, owner_id, title, category, body_md, trigger_ctx, tags,
                      active, source_session_id, source_msg_ref,
                      created_at, updated_at, retired_at
            """,
            (rule_id, owner_id),
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    return _serialize(_row(row, _RULE_COLS))


def get_rule(conn: _Connection, rule_id: str, owner_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, owner_id, title, category, body_md, trigger_ctx, tags,
                   active, source_session_id, source_msg_ref,
                   created_at, updated_at, retired_at
            FROM {TABLE_RESEARCH_PLAYBOOK_RULE}
            WHERE id = %s::uuid AND owner_id = %s
            """,
            (rule_id, owner_id),
        )
        row = cur.fetchone()
    if not row:
        return None
    return _serialize(_row(row, _RULE_COLS))


def list_notes(
    conn: _Connection,
    *,
    owner_id: str,
    symbol: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses = ["owner_id = %s"]
    params: list[Any] = [owner_id]
    if symbol:
        clauses.append("%s = ANY(symbols)")
        params.append(symbol)
    params.append(limit)
    where = " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, owner_id, note_md, tags, symbols, source_session_id, created_at
            FROM {TABLE_RESEARCH_PLAYBOOK_NOTE}
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        row = _row(r, ("id", "owner_id", "note_md", "tags", "symbols", "source_session_id", "created_at"))
        if row.get("id"):
            row["id"] = str(row["id"])
        if isinstance(row.get("created_at"), datetime):
            row["created_at"] = row["created_at"].isoformat()
        out.append(row)
    return out


def create_note(
    conn: _Connection,
    *,
    owner_id: str,
    note_md: str,
    tags: list[str] | None = None,
    symbols: list[str] | None = None,
    source_session_id: str | None = None,
) -> dict[str, Any]:
    nid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_RESEARCH_PLAYBOOK_NOTE}
                (id, owner_id, note_md, tags, symbols, source_session_id)
            VALUES (%s::uuid, %s, %s, %s::text[], %s::text[], %s::uuid)
            RETURNING id, owner_id, note_md, tags, symbols, source_session_id, created_at
            """,
            (nid, owner_id, note_md, tags or [], symbols or [], source_session_id),
        )
        row = cur.fetchone()
    conn.commit()
    row = _row(row, ("id", "owner_id", "note_md", "tags", "symbols", "source_session_id", "created_at"))
    row["id"] = str(row["id"])
    if isinstance(row.get("created_at"), datetime):
        row["created_at"] = row["created_at"].isoformat()
    return row


def list_cases(conn: _Connection, *, owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, owner_id, trade_ref, outcome, lessons_md, tags,
                   related_rule_ids, created_at
            FROM {TABLE_RESEARCH_PLAYBOOK_CASE}
            WHERE owner_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (owner_id, limit),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        row = _serialize(
            _row(r, ("id", "owner_id", "trade_ref", "outcome", "lessons_md", "tags", "related_rule_ids", "created_at"))
        )
        out.append(row)
    return out


def create_case(
    conn: _Connection,
    *,
    owner_id: str,
    lessons_md: str,
    trade_ref: dict[str, Any] | None = None,
    outcome: str | None = None,
    tags: list[str] | None = None,
    related_rule_ids: list[str] | None = None,
) -> dict[str, Any]:
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_RESEARCH_PLAYBOOK_CASE}
                (id, owner_id, trade_ref, outcome, lessons_md, tags, related_rule_ids)
            VALUES (%s::uuid, %s, %s::jsonb, %s, %s, %s::text[], %s::uuid[])
            RETURNING id, owner_id, trade_ref, outcome, lessons_md, tags,
                      related_rule_ids, created_at
            """,
            (
                cid,
                owner_id,
                json.dumps(trade_ref or {}),
                outcome,
                lessons_md,
                tags or [],
                related_rule_ids or [],
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return _serialize(
        _row(row, ("id", "owner_id", "trade_ref", "outcome", "lessons_md", "tags", "related_rule_ids", "created_at"))
    )


def create_case_from_bridge(
    conn: _Connection,
    *,
    owner_id: str,
    bridge_event_id: str,
    external_reply_md: str,
    outcome: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any] | None:
    """Save external AI reply as playbook case (RS-EX3 feedback loop)."""
    from bifrost_research.schema.schemas import TABLE_RESEARCH_COPILOT_BRIDGE_EVENT

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, session_id, focus, target, preview_md
            FROM {TABLE_RESEARCH_COPILOT_BRIDGE_EVENT}
            WHERE id = %s::uuid AND owner_id = %s
            """,
            (bridge_event_id, owner_id),
        )
        bridge = cur.fetchone()
    if not bridge:
        return None
    if isinstance(bridge, dict):
        bridge_row = bridge
    else:
        bridge_row = {
            "id": bridge[0],
            "session_id": bridge[1],
            "focus": bridge[2],
            "target": bridge[3],
            "preview_md": bridge[4],
        }
    merged_tags = list(tags or [])
    if "bridge-feedback" not in merged_tags:
        merged_tags.append("bridge-feedback")
    lessons = (
        "## External AI reply\n\n"
        f"{external_reply_md.strip()}\n\n"
        "## Bridge context (snapshot)\n\n"
        f"{str(bridge_row.get('preview_md') or '')[:4000]}"
    )
    trade_ref = {
        "source": "bridge_feedback",
        "bridge_event_id": str(bridge_row.get("id")),
        "session_id": str(bridge_row.get("session_id")),
        "focus": bridge_row.get("focus"),
        "target": bridge_row.get("target"),
        "external_reply_md": external_reply_md.strip()[:8000],
    }
    return create_case(
        conn,
        owner_id=owner_id,
        lessons_md=lessons,
        trade_ref=trade_ref,
        outcome=outcome or "Bridge feedback",
        tags=merged_tags,
    )


def search_keyword(
    conn: _Connection,
    *,
    owner_id: str,
    query: str,
    limit: int = 20,
) -> dict[str, Any]:
    """RS-KB5 keyword fallback when pgvector is unavailable."""
    q = f"%{query.strip()}%"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, title, category, body_md, tags, updated_at
            FROM {TABLE_RESEARCH_PLAYBOOK_RULE}
            WHERE owner_id = %s AND active = true
              AND (title ILIKE %s OR body_md ILIKE %s)
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (owner_id, q, q, limit),
        )
        rules = cur.fetchall()
        cur.execute(
            f"""
            SELECT id, note_md, symbols, created_at
            FROM {TABLE_RESEARCH_PLAYBOOK_NOTE}
            WHERE owner_id = %s AND note_md ILIKE %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (owner_id, q, limit),
        )
        notes = cur.fetchall()
    return {
        "rules": [_serialize(_row(r, _RULE_COLS[:6] + ("tags", "updated_at"))) for r in rules],
        "notes": [
            {
                "id": str(r[0]),
                "note_md": r[1],
                "symbols": r[2],
                "created_at": r[3].isoformat() if isinstance(r[3], datetime) else r[3],
            }
            for r in notes
        ],
    }


__all__ = [
    "create_case",
    "create_case_from_bridge",
    "create_note",
    "create_rule",
    "get_rule",
    "list_cases",
    "list_notes",
    "list_rules",
    "retire_rule",
    "search_keyword",
    "update_rule",
]
