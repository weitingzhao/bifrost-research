"""SQL layer for ``research.ai_action_log`` — Wave RS-E3 (D-RS-E-g)."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from bifrost_research.schema.schemas import TABLE_RESEARCH_AI_ACTION_LOG

_ALLOWED_STATUSES = frozenset(
    {"proposed", "approved", "rejected", "executed", "error"}
)

_COLUMNS: tuple[str, ...] = (
    "id",
    "session_id",
    "action_kind",
    "action_source",
    "model",
    "input",
    "output",
    "tool_calls",
    "status",
    "approved_by",
    "approved_at",
    "executed_at",
    "executed_result",
    "created_at",
)

_JSON_COLS = frozenset({"input", "output", "tool_calls", "executed_result"})
_TS_COLS = frozenset({"approved_at", "executed_at", "created_at"})


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def generate_action_id() -> str:
    ts = int(time.time() * 1000)
    return f"aal_{ts:x}{secrets.token_hex(4)}"


def _validate_status(status: str) -> str:
    if status not in _ALLOWED_STATUSES:
        raise ValueError(f"invalid ai_action_log status: {status!r}")
    return status


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


def insert_action(
    conn: _Connection,
    *,
    action_kind: str,
    action_source: str,
    model: str | None = None,
    input_payload: Any = None,
    output_payload: Any = None,
    tool_calls: Any = None,
    status: str = "proposed",
    session_id: str | None = None,
    action_id: str | None = None,
) -> dict[str, Any]:
    aid = (action_id or generate_action_id()).strip()
    validated = _validate_status(status)
    sql = f"""
        INSERT INTO {TABLE_RESEARCH_AI_ACTION_LOG} (
            id, session_id, action_kind, action_source, model,
            input, output, tool_calls, status
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING {_cols()}
    """
    params = (
        aid,
        session_id,
        action_kind.strip(),
        action_source.strip(),
        model,
        _serialize_json(input_payload),
        _serialize_json(output_payload),
        _serialize_json(tool_calls),
        validated,
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
        raise RuntimeError("insert ai_action_log returned no row")
    return _row_to_dict(row)


def get_action(conn: _Connection, action_id: str) -> dict[str, Any] | None:
    sql = f"""
        SELECT {_cols()}
        FROM {TABLE_RESEARCH_AI_ACTION_LOG}
        WHERE id = %s
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (action_id,))
        row = cur.fetchone()
    return _row_to_dict(row) if row is not None else None


def update_action_status(
    conn: _Connection,
    action_id: str,
    *,
    status: str,
    approved_by: str | None = None,
    executed_result: Any = None,
) -> dict[str, Any] | None:
    validated = _validate_status(status)
    sets = ["status = %s"]
    params: list[Any] = [validated]
    if status in {"approved", "rejected"} and approved_by is not None:
        sets.append("approved_by = %s")
        params.append(approved_by)
        sets.append("approved_at = now()")
    if status == "executed":
        sets.append("executed_at = now()")
        if approved_by is not None:
            sets.append("approved_by = COALESCE(approved_by, %s)")
            params.append(approved_by)
            sets.append("approved_at = COALESCE(approved_at, now())")
        if executed_result is not None:
            sets.append("executed_result = %s")
            params.append(_serialize_json(executed_result))
    elif executed_result is not None:
        sets.append("executed_result = %s")
        params.append(_serialize_json(executed_result))
    sql = f"""
        UPDATE {TABLE_RESEARCH_AI_ACTION_LOG}
        SET {", ".join(sets)}
        WHERE id = %s
        RETURNING {_cols()}
    """
    params.append(action_id)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _row_to_dict(row) if row is not None else None


def list_actions(
    conn: _Connection,
    *,
    status: str | None = None,
    action_source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        _validate_status(status)
        clauses.append("status = %s")
        params.append(status)
    if action_source:
        clauses.append("action_source = %s")
        params.append(action_source)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT {_cols()}
        FROM {TABLE_RESEARCH_AI_ACTION_LOG}
        {where}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([int(limit), int(offset)])
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []
    return [_row_to_dict(r) for r in rows]
