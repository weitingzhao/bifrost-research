"""SQL layer for ``research.loop_policy_template`` — P0-2 editable Loop policy.

The "recommended policy template" used to be a hardcoded constant in two places at
once: ``default_stock_composite_policy()`` in Python and RECOMMENDED_LOOP_POLICY /
RECOMMENDED_LOOP_POLICY_STOCK in the frontend. Tuning the strategy meant a release,
and the two copies were free to drift — the frontend one still carried
``min_composite_score: 0.55`` against a 0–100 scale, which filters nothing.

Every write validates through ``LoopPolicy`` so a template cannot store a shape the
runtime will not honour.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from bifrost_research.copilot.harness.policy_schema import (
    LoopPolicy,
    validate_policy_for_mode,
)
from bifrost_research.schema.schemas import (
    TABLE_RESEARCH_LOOP_POLICY_TEMPLATE,
    TABLE_RESEARCH_OBJECTIVE,
)

_COLS: tuple[str, ...] = (
    "id",
    "name",
    "description",
    "universe_mode",
    "policy_json",
    "is_default",
    "owner_id",
    "created_at",
    "updated_at",
)

_JSON_COLS = frozenset({"policy_json"})
_TS_COLS = frozenset({"created_at", "updated_at"})


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def generate_template_id() -> str:
    return f"lpt_{int(time.time() * 1000):x}{secrets.token_hex(4)}"


def _cols() -> str:
    return ", ".join(_COLS)


def _serialize_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    return dt.isoformat() if isinstance(dt, datetime) else str(dt)


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        out = {c: row[c] for c in _COLS if c in row}
    else:
        out = {_COLS[i]: row[i] for i in range(min(len(_COLS), len(row)))}
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


class PolicyValidationError(ValueError):
    """Raised when a template body does not parse as a LoopPolicy."""


def validate_policy(policy: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Return ``(normalised_policy, warnings)`` or raise PolicyValidationError.

    Reuses the runtime's own model rather than a second schema: a template that
    passes here is a template the runtime honours. ``validate_policy_for_mode``
    adds the non-fatal notes — e.g. min_hit_rate being ignored in stock modes
    without a flag_filter — which the console shows instead of swallowing.
    """
    try:
        parsed = LoopPolicy.model_validate(dict(policy or {}))
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as 400
        raise PolicyValidationError(str(exc)) from exc
    return parsed.model_dump(), validate_policy_for_mode(parsed)


def list_templates(
    conn: _Connection,
    *,
    universe_mode: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if universe_mode:
        clauses.append("universe_mode = %s")
        params.append(universe_mode)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT {_cols()} FROM {TABLE_RESEARCH_LOOP_POLICY_TEMPLATE}
        {where}
        ORDER BY is_default DESC, name ASC
    """
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []
    return [_row_to_dict(r) for r in rows]


def get_template(conn: _Connection, template_id: str) -> dict[str, Any] | None:
    sql = f"SELECT {_cols()} FROM {TABLE_RESEARCH_LOOP_POLICY_TEMPLATE} WHERE id = %s LIMIT 1"
    with conn.cursor() as cur:
        cur.execute(sql, (template_id,))
        row = cur.fetchone()
    return _row_to_dict(row) if row is not None else None


def create_template(
    conn: _Connection,
    *,
    name: str,
    policy_json: Mapping[str, Any],
    description: str = "",
    is_default: bool = False,
    owner_id: str = "owner",
    template_id: str | None = None,
) -> dict[str, Any]:
    if not name or not name.strip():
        raise ValueError("name is required")
    normalised, _warnings = validate_policy(policy_json)
    tid = (template_id or generate_template_id()).strip()
    sql = f"""
        INSERT INTO {TABLE_RESEARCH_LOOP_POLICY_TEMPLATE}
            (id, name, description, universe_mode, policy_json, is_default, owner_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING {_cols()}
    """
    params = (
        tid,
        name.strip(),
        str(description or "").strip(),
        str(normalised.get("universe_mode") or "scan_legacy"),
        _serialize_json(normalised),
        bool(is_default),
        str(owner_id or "owner"),
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
        raise RuntimeError("insert loop_policy_template returned no row")
    return _row_to_dict(row)


def update_template(
    conn: _Connection,
    template_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    policy_json: Mapping[str, Any] | None = None,
    is_default: bool | None = None,
) -> dict[str, Any] | None:
    sets: list[str] = []
    params: list[Any] = []
    if name is not None:
        if not name.strip():
            raise ValueError("name cannot be blank")
        sets.append("name = %s")
        params.append(name.strip())
    if description is not None:
        sets.append("description = %s")
        params.append(str(description).strip())
    if policy_json is not None:
        normalised, _warnings = validate_policy(policy_json)
        sets.append("policy_json = %s")
        params.append(_serialize_json(normalised))
        sets.append("universe_mode = %s")
        params.append(str(normalised.get("universe_mode") or "scan_legacy"))
    if is_default is not None:
        sets.append("is_default = %s")
        params.append(bool(is_default))
    if not sets:
        return get_template(conn, template_id)

    sets.append("updated_at = now()")
    sql = f"""
        UPDATE {TABLE_RESEARCH_LOOP_POLICY_TEMPLATE}
        SET {", ".join(sets)}
        WHERE id = %s
        RETURNING {_cols()}
    """
    params.append(template_id)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _row_to_dict(row) if row is not None else None


def count_objectives_using(conn: _Connection, template_id: str) -> int:
    """Objectives created from this template.

    Nothing in the schema enforces this link — objectives copy policy_json at
    creation and carry the source in it — so the check has to live here or a
    delete would quietly orphan the provenance.
    """
    sql = f"""
        SELECT COUNT(*) FROM {TABLE_RESEARCH_OBJECTIVE}
        WHERE policy_json ->> 'source_template_id' = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (template_id,))
        row = cur.fetchone()
    if row is None:
        return 0
    if isinstance(row, Mapping):
        return int(next(iter(row.values())) or 0)
    return int(row[0] or 0)


def delete_template(conn: _Connection, template_id: str) -> bool:
    sql = f"DELETE FROM {TABLE_RESEARCH_LOOP_POLICY_TEMPLATE} WHERE id = %s"
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (template_id,))
            deleted = getattr(cur, "rowcount", 0) or 0
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return deleted > 0
