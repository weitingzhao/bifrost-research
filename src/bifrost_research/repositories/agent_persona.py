"""SQL layer for research.agent_persona — Wave RS-PS1."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from bifrost_research.copilot.agents.agent_catalog import AGENT_NAMES, GUARDRAIL_LOCKED_AGENTS
from bifrost_research.schema.schemas import TABLE_RESEARCH_AGENT_PERSONA

_PERSONAS_DIR = Path(__file__).resolve().parent.parent / "copilot" / "agents" / "personas"


class _Connection(Protocol):
    def cursor(self) -> Any: ...
    def commit(self) -> None: ...


_COLS = (
    "owner_id",
    "agent_name",
    "persona_md",
    "preferences_json",
    "guardrail_locked",
    "seeded",
    "updated_at",
)


def _row(row: Any, cols: tuple[str, ...]) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return {cols[i]: row[i] for i in range(min(len(cols), len(row)))}


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    prefs = out.get("preferences_json")
    if isinstance(prefs, str):
        out["preferences_json"] = json.loads(prefs)
    val = out.get("updated_at")
    if isinstance(val, datetime):
        out["updated_at"] = val.isoformat()
    return out


def _default_persona_md(agent_name: str) -> str:
    path = _PERSONAS_DIR / f"{agent_name}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return f"# {agent_name} persona\n\n(Default — customize in Agent Personas.)"


def seed_defaults_if_missing(conn: _Connection, owner_id: str) -> int:
    inserted = 0
    for name in AGENT_NAMES:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT 1 FROM {TABLE_RESEARCH_AGENT_PERSONA}
                WHERE owner_id = %s AND agent_name = %s
                """,
                (owner_id, name),
            )
            if cur.fetchone():
                continue
            locked = name in GUARDRAIL_LOCKED_AGENTS
            cur.execute(
                f"""
                INSERT INTO {TABLE_RESEARCH_AGENT_PERSONA}
                    (owner_id, agent_name, persona_md, preferences_json,
                     guardrail_locked, seeded, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, %s, true, now())
                """,
                (
                    owner_id,
                    name,
                    _default_persona_md(name),
                    json.dumps({}),
                    locked,
                ),
            )
            inserted += 1
    if inserted:
        conn.commit()
    return inserted


def list_for_owner(conn: _Connection, owner_id: str) -> list[dict[str, Any]]:
    seed_defaults_if_missing(conn, owner_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT owner_id, agent_name, persona_md, preferences_json,
                   guardrail_locked, seeded, updated_at
            FROM {TABLE_RESEARCH_AGENT_PERSONA}
            WHERE owner_id = %s
            ORDER BY agent_name
            """,
            (owner_id,),
        )
        rows = cur.fetchall()
    return [_serialize(_row(r, _COLS)) for r in rows]


def get(conn: _Connection, owner_id: str, agent_name: str) -> dict[str, Any] | None:
    seed_defaults_if_missing(conn, owner_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT owner_id, agent_name, persona_md, preferences_json,
                   guardrail_locked, seeded, updated_at
            FROM {TABLE_RESEARCH_AGENT_PERSONA}
            WHERE owner_id = %s AND agent_name = %s
            """,
            (owner_id, agent_name),
        )
        row = cur.fetchone()
    if not row:
        return None
    return _serialize(_row(row, _COLS))


def upsert(
    conn: _Connection,
    owner_id: str,
    agent_name: str,
    *,
    persona_md: str | None = None,
    preferences_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if agent_name not in AGENT_NAMES:
        raise ValueError(f"invalid agent_name: {agent_name}")
    existing = get(conn, owner_id, agent_name)
    if existing is None:
        seed_defaults_if_missing(conn, owner_id)
        existing = get(conn, owner_id, agent_name)
    locked = bool(existing and existing.get("guardrail_locked"))
    new_md = persona_md if persona_md is not None else (existing or {}).get("persona_md", "")
    new_prefs = preferences_json if preferences_json is not None else (existing or {}).get(
        "preferences_json", {}
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_RESEARCH_AGENT_PERSONA}
                (owner_id, agent_name, persona_md, preferences_json,
                 guardrail_locked, seeded, updated_at)
            VALUES (%s, %s, %s, %s::jsonb, %s, false, now())
            ON CONFLICT (owner_id, agent_name) DO UPDATE SET
                persona_md = EXCLUDED.persona_md,
                preferences_json = EXCLUDED.preferences_json,
                updated_at = now()
            RETURNING owner_id, agent_name, persona_md, preferences_json,
                      guardrail_locked, seeded, updated_at
            """,
            (
                owner_id,
                agent_name,
                new_md,
                json.dumps(new_prefs or {}),
                locked,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return _serialize(_row(row, _COLS))


def reset(conn: _Connection, owner_id: str, agent_name: str) -> dict[str, Any]:
    if agent_name not in AGENT_NAMES:
        raise ValueError(f"invalid agent_name: {agent_name}")
    locked = agent_name in GUARDRAIL_LOCKED_AGENTS
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_RESEARCH_AGENT_PERSONA}
                (owner_id, agent_name, persona_md, preferences_json,
                 guardrail_locked, seeded, updated_at)
            VALUES (%s, %s, %s, %s::jsonb, %s, true, now())
            ON CONFLICT (owner_id, agent_name) DO UPDATE SET
                persona_md = EXCLUDED.persona_md,
                preferences_json = '{{}}'::jsonb,
                seeded = true,
                updated_at = now()
            RETURNING owner_id, agent_name, persona_md, preferences_json,
                      guardrail_locked, seeded, updated_at
            """,
            (
                owner_id,
                agent_name,
                _default_persona_md(agent_name),
                locked,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return _serialize(_row(row, _COLS))


def apply_preference_diff(
    conn: _Connection,
    owner_id: str,
    agent_name: str,
    diff: dict[str, Any],
) -> dict[str, Any]:
    """Merge persona_diff keys into preferences_json (Wave RS-PS3)."""
    if agent_name not in AGENT_NAMES:
        raise ValueError(f"invalid agent_name: {agent_name}")
    existing = get(conn, owner_id, agent_name) or {}
    prefs = dict(existing.get("preferences_json") or {})
    for key, val in diff.items():
        prefs[key] = val
    return upsert(conn, owner_id, agent_name, preferences_json=prefs)


def persona_snapshot_lines(
    conn: _Connection,
    owner_id: str,
    *,
    max_chars_per_agent: int = 80,
) -> list[str]:
    """Short lines for bridge / export headers."""
    rows = list_for_owner(conn, owner_id)
    lines: list[str] = []
    for r in rows:
        name = r.get("agent_name", "")
        md = str(r.get("persona_md") or "").replace("\n", " ").strip()
        if len(md) > max_chars_per_agent:
            md = md[: max_chars_per_agent - 1] + "…"
        lines.append(f"- **{name}**: {md}")
    return lines


__all__ = [
    "apply_preference_diff",
    "get",
    "list_for_owner",
    "persona_snapshot_lines",
    "reset",
    "seed_defaults_if_missing",
    "upsert",
]
