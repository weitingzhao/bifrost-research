"""SQL layer for ``research.objective`` + ``objective_run`` — Wave A Harness."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from bifrost_research.schema.schemas import (
    TABLE_RESEARCH_OBJECTIVE,
    TABLE_RESEARCH_OBJECTIVE_RUN,
)

_ALLOWED_SCHEDULES = frozenset({"daily_open", "daily_eod", "weekly", "adhoc"})
_ALLOWED_OBJ_STATUSES = frozenset({"active", "paused", "retired"})
_ALLOWED_RUN_STATUSES = frozenset(
    {"running", "awaiting_approval", "completed", "failed", "cancelled"}
)

_OBJ_COLS: tuple[str, ...] = (
    "id",
    "title",
    "description",
    "schedule",
    "policy_json",
    "persona",
    "status",
    "owner_id",
    "created_at",
)

_RUN_COLS: tuple[str, ...] = (
    "id",
    "objective_id",
    "started_at",
    "finished_at",
    "plan_json",
    "trace_json",
    "outputs",
    "status",
)


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def generate_objective_id(title: str) -> str:
    base = "".join(c if c.isalnum() else "-" for c in (title or "obj").lower())[:32].strip("-")
    ts = int(time.time() * 1000) & 0xFFFFFF
    return f"obj-{base or 'obj'}-{ts:06x}{secrets.token_hex(2)}"


def generate_run_id() -> str:
    ts = int(time.time() * 1000)
    return f"run_{ts:x}{secrets.token_hex(3)}"


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


def _obj_row(row: Sequence[Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for i, col in enumerate(_OBJ_COLS):
        val = row[i]
        if col == "policy_json":
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except json.JSONDecodeError:
                    val = {}
            elif val is None:
                val = {}
        elif col == "created_at":
            val = _iso(val)
        out[col] = val
    return out


def _run_row(row: Sequence[Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for i, col in enumerate(_RUN_COLS):
        val = row[i]
        if col in {"plan_json", "trace_json", "outputs"}:
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except json.JSONDecodeError:
                    val = {}
            elif val is None:
                val = {}
        elif col in {"started_at", "finished_at"}:
            val = _iso(val)
        out[col] = val
    return out


def create_objective(
    conn: _Connection,
    *,
    title: str,
    description: str,
    schedule: str = "adhoc",
    policy_json: Mapping[str, Any] | None = None,
    persona: str = "loop_curator",
    owner_id: str = "owner",
    objective_id: str | None = None,
) -> dict[str, Any]:
    sched = (schedule or "adhoc").strip().lower()
    if sched not in _ALLOWED_SCHEDULES:
        raise ValueError(f"invalid schedule: {schedule!r}")
    oid = objective_id or generate_objective_id(title)
    sql = f"""
        INSERT INTO {TABLE_RESEARCH_OBJECTIVE} (
            id, title, description, schedule, policy_json, persona, status, owner_id
        ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, 'active', %s)
        RETURNING {", ".join(_OBJ_COLS)}
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                oid,
                title.strip(),
                description.strip(),
                sched,
                _serialize_json(dict(policy_json or {})),
                persona,
                owner_id,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    result = _obj_row(row)
    assert result is not None
    return result


def get_objective(conn: _Connection, objective_id: str) -> dict[str, Any] | None:
    sql = f"SELECT {', '.join(_OBJ_COLS)} FROM {TABLE_RESEARCH_OBJECTIVE} WHERE id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (objective_id,))
        return _obj_row(cur.fetchone())


POLICY_SUGGESTION_WHITELIST: frozenset[str] = frozenset(
    {
        "preset",
        "flag_filter",
        "min_composite_score",
        "min_hit_rate",
        "max_candidates",
        "universe_mode",
        "layers",
        "option_overlay",
    }
)

_NESTED_POLICY_KEYS = frozenset({"layers", "option_overlay"})


def _deep_merge_policy_patch(
    current: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Merge whitelist-filtered patch onto current policy (nested layers/overlay)."""
    merged = dict(current)
    for key, value in patch.items():
        if key in _NESTED_POLICY_KEYS and isinstance(value, dict):
            base_nested = merged.get(key)
            if not isinstance(base_nested, dict):
                base_nested = {}
            nested_out = dict(base_nested)
            for sub_key, sub_val in value.items():
                if isinstance(sub_val, dict) and isinstance(nested_out.get(sub_key), dict):
                    nested_out[sub_key] = {**nested_out[sub_key], **sub_val}
                else:
                    nested_out[sub_key] = sub_val
            merged[key] = nested_out
        else:
            merged[key] = value
    return merged


def patch_policy_json(
    conn: _Connection,
    objective_id: str,
    patch: Mapping[str, Any],
    *,
    whitelist: frozenset[str] | None = POLICY_SUGGESTION_WHITELIST,
) -> dict[str, Any] | None:
    """Merge ``patch`` (whitelist-filtered) onto ``objective.policy_json``.

    Wave Y.3 + LS-1: nested ``layers`` / ``option_overlay`` merge deeply.
    """
    if not isinstance(patch, Mapping):
        raise TypeError("patch must be a mapping")
    filtered: dict[str, Any] = {}
    if whitelist is None:
        filtered.update(dict(patch))
    else:
        for k, v in patch.items():
            if k in whitelist:
                filtered[k] = v
    if not filtered:
        return get_objective(conn, objective_id)

    current = get_objective(conn, objective_id)
    if current is None:
        return None
    current_policy = current.get("policy_json") or {}
    if not isinstance(current_policy, dict):
        current_policy = {}
    merged_policy = _deep_merge_policy_patch(current_policy, filtered)

    sql = f"""
        UPDATE {TABLE_RESEARCH_OBJECTIVE}
        SET policy_json = %s::jsonb
        WHERE id = %s
        RETURNING {", ".join(_OBJ_COLS)}
    """
    with conn.cursor() as cur:
        cur.execute(sql, (_serialize_json(merged_policy), objective_id))
        row = cur.fetchone()
    conn.commit()
    return _obj_row(row)


def list_objectives(
    conn: _Connection,
    *,
    status: str | None = "active",
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT {", ".join(_OBJ_COLS)}
        FROM {TABLE_RESEARCH_OBJECTIVE}
        {where}
        ORDER BY created_at DESC
        LIMIT %s
    """
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [_obj_row(r) for r in cur.fetchall() if r]  # type: ignore[misc]


def create_run(
    conn: _Connection,
    *,
    objective_id: str,
    plan_json: Mapping[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    rid = run_id or generate_run_id()
    sql = f"""
        INSERT INTO {TABLE_RESEARCH_OBJECTIVE_RUN} (
            id, objective_id, plan_json, status
        ) VALUES (%s, %s, %s::jsonb, 'running')
        RETURNING {", ".join(_RUN_COLS)}
    """
    with conn.cursor() as cur:
        cur.execute(sql, (rid, objective_id, _serialize_json(dict(plan_json or {}))))
        row = cur.fetchone()
    conn.commit()
    result = _run_row(row)
    assert result is not None
    return result


def get_run(conn: _Connection, run_id: str) -> dict[str, Any] | None:
    sql = f"SELECT {', '.join(_RUN_COLS)} FROM {TABLE_RESEARCH_OBJECTIVE_RUN} WHERE id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (run_id,))
        return _run_row(cur.fetchone())


def list_runs(
    conn: _Connection,
    *,
    status: str | None = None,
    objective_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        if status not in _ALLOWED_RUN_STATUSES:
            raise ValueError(f"invalid run status: {status!r}")
        clauses.append("status = %s")
        params.append(status)
    if objective_id:
        clauses.append("objective_id = %s")
        params.append(objective_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT {", ".join(_RUN_COLS)}
        FROM {TABLE_RESEARCH_OBJECTIVE_RUN}
        {where}
        ORDER BY started_at DESC
        LIMIT %s
    """
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [_run_row(r) for r in cur.fetchall() if r]  # type: ignore[misc]


def finish_run(
    conn: _Connection,
    run_id: str,
    *,
    status: str,
    trace_json: Mapping[str, Any] | None = None,
    outputs: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if status not in _ALLOWED_RUN_STATUSES:
        raise ValueError(f"invalid run status: {status!r}")
    sql = f"""
        UPDATE {TABLE_RESEARCH_OBJECTIVE_RUN}
        SET status = %s,
            finished_at = now(),
            trace_json = COALESCE(%s::jsonb, trace_json),
            outputs = COALESCE(%s::jsonb, outputs)
        WHERE id = %s
        RETURNING {", ".join(_RUN_COLS)}
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                status,
                _serialize_json(dict(trace_json) if trace_json is not None else None),
                _serialize_json(dict(outputs) if outputs is not None else None),
                run_id,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return _run_row(row)


def update_run_status(conn: _Connection, run_id: str, *, status: str) -> dict[str, Any] | None:
    return finish_run(conn, run_id, status=status)


def patch_run_outputs(
    conn: _Connection,
    run_id: str,
    patch: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Merge ``patch`` into ``objective_run.outputs`` (jsonb ||)."""
    sql = f"""
        UPDATE {TABLE_RESEARCH_OBJECTIVE_RUN}
        SET outputs = COALESCE(outputs, '{{}}'::jsonb) || %s::jsonb
        WHERE id = %s
        RETURNING {", ".join(_RUN_COLS)}
    """
    with conn.cursor() as cur:
        cur.execute(sql, (_serialize_json(dict(patch)), run_id))
        row = cur.fetchone()
    conn.commit()
    return _run_row(row)
