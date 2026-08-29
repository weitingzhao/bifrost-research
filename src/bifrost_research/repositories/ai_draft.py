"""SQL layer for ``research.ai_draft`` — Wave RS-E3 Cockpit inbox (D-RS-E-e)."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from bifrost_research.schema.schemas import TABLE_RESEARCH_AI_DRAFT

_ALLOWED_STATUSES = frozenset({"pending", "approved", "dismissed", "expired"})
_ALLOWED_KINDS = frozenset(
    {
        "morning_brief",
        "eod_verdict",
        "hypothesis_suggestion",
        "playbook_rule",
        "playbook_note",
        # Wave C / A / O — Research Loop
        "candidate_batch",
        "hypothesis_draft",
        "decision_draft",
        "order_intent",
    }
)

_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "payload",
    "scope",
    "status",
    "generated_by",
    "linked_action_id",
    "created_at",
    "expires_at",
)

_JSON_COLS = frozenset({"payload"})
_TS_COLS = frozenset({"created_at", "expires_at"})


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def generate_draft_id() -> str:
    ts = int(time.time() * 1000)
    return f"drf_{ts:x}{secrets.token_hex(4)}"


def _validate_status(status: str) -> str:
    if status not in _ALLOWED_STATUSES:
        raise ValueError(f"invalid ai_draft status: {status!r}")
    return status


def _validate_kind(kind: str) -> str:
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"invalid ai_draft kind: {kind!r}")
    return kind


def _serialize_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        out = {col: row[col] for col in _COLUMNS if col in row}
    else:
        out = {_COLUMNS[i]: row[i] for i in range(min(len(_COLUMNS), len(row)))}
    for col in _JSON_COLS:
        val = out.get(col)
        if isinstance(val, (bytes, bytearray)):
            val = val.decode("utf-8", errors="replace")
        if isinstance(val, str):
            try:
                out[col] = json.loads(val)
            except Exception:
                out[col] = val
    for col in _TS_COLS:
        if col in out:
            out[col] = _iso(out[col])
    return out


def _cols() -> str:
    return ", ".join(_COLUMNS)


def insert_draft(
    conn: _Connection,
    *,
    kind: str,
    payload: Any,
    scope: str,
    generated_by: str,
    linked_action_id: str | None = None,
    status: str = "pending",
    expires_at: Any = None,
    draft_id: str | None = None,
) -> dict[str, Any]:
    did = (draft_id or generate_draft_id()).strip()
    validated_kind = _validate_kind(kind)
    validated_status = _validate_status(status)
    if not scope or not str(scope).strip():
        raise ValueError("scope is required")
    if not generated_by or not str(generated_by).strip():
        raise ValueError("generated_by is required")
    if payload is None:
        raise ValueError("payload is required")

    sql = f"""
        INSERT INTO {TABLE_RESEARCH_AI_DRAFT} (
            id, kind, payload, scope, status, generated_by,
            linked_action_id, expires_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING {_cols()}
    """
    params = (
        did,
        validated_kind,
        _serialize_json(payload),
        str(scope).strip(),
        validated_status,
        str(generated_by).strip(),
        linked_action_id,
        expires_at,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row is None:
        raise RuntimeError("insert ai_draft returned no row")
    return _row_to_dict(row)


def get_draft(conn: _Connection, draft_id: str) -> dict[str, Any] | None:
    sql = f"""
        SELECT {_cols()}
        FROM {TABLE_RESEARCH_AI_DRAFT}
        WHERE id = %s
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (draft_id,))
        row = cur.fetchone()
    return _row_to_dict(row) if row is not None else None


def list_drafts(
    conn: _Connection,
    *,
    status: str | None = "pending",
    kind: str | None = None,
    scope: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        _validate_status(status)
        clauses.append("status = %s")
        params.append(status)
    if kind:
        _validate_kind(kind)
        clauses.append("kind = %s")
        params.append(kind)
    if scope:
        clauses.append("scope = %s")
        params.append(scope)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT {_cols()}
        FROM {TABLE_RESEARCH_AI_DRAFT}
        {where}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([int(limit), int(offset)])
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []
    return [_row_to_dict(r) for r in rows]


def count_pending(conn: _Connection, *, kind: str | None = None) -> int:
    clauses = ["status = 'pending'"]
    params: list[Any] = []
    if kind:
        _validate_kind(kind)
        clauses.append("kind = %s")
        params.append(kind)
    sql = f"""
        SELECT COUNT(*)
        FROM {TABLE_RESEARCH_AI_DRAFT}
        WHERE {' AND '.join(clauses)}
    """
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
    if row is None:
        return 0
    if isinstance(row, Mapping):
        return int(next(iter(row.values())))
    return int(row[0] or 0)


def update_draft_status(
    conn: _Connection,
    draft_id: str,
    *,
    status: str,
) -> dict[str, Any] | None:
    validated = _validate_status(status)
    sql = f"""
        UPDATE {TABLE_RESEARCH_AI_DRAFT}
        SET status = %s
        WHERE id = %s
        RETURNING {_cols()}
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (validated, draft_id))
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _row_to_dict(row) if row is not None else None
