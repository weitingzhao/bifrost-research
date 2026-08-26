"""SQL layer for ``research.copilot_bridge_event`` — Wave RS-EX2."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Protocol

from bifrost_research.schema.schemas import TABLE_RESEARCH_COPILOT_BRIDGE_EVENT

_EVENT_COLS = (
    "id",
    "owner_id",
    "session_id",
    "focus",
    "depth",
    "target",
    "model",
    "frames_from_message_id",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "preview_md",
    "polished",
    "created_at",
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
    if out.get("session_id") is not None:
        out["session_id"] = str(out["session_id"])
    for col in ("created_at",):
        val = out.get(col)
        if isinstance(val, datetime):
            out[col] = val.isoformat()
    if out.get("cost_usd") is not None:
        out["cost_usd"] = float(out["cost_usd"])
    return out


def insert_event(
    conn: _Connection,
    *,
    owner_id: str,
    session_id: str,
    focus: str,
    depth: str,
    target: str,
    model: str,
    frames_from_message_id: str | None,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    preview_md: str,
    polished: bool,
) -> dict[str, Any]:
    eid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_RESEARCH_COPILOT_BRIDGE_EVENT}
                (id, owner_id, session_id, focus, depth, target, model,
                 frames_from_message_id, input_tokens, output_tokens, cost_usd,
                 preview_md, polished)
            VALUES (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, owner_id, session_id, focus, depth, target, model,
                      frames_from_message_id, input_tokens, output_tokens, cost_usd,
                      preview_md, polished, created_at
            """,
            (
                eid,
                owner_id,
                session_id,
                focus,
                depth,
                target,
                model,
                frames_from_message_id,
                input_tokens,
                output_tokens,
                cost_usd,
                preview_md,
                polished,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return _serialize(_row(row, _EVENT_COLS))


def usage_stats_today(conn: _Connection, *, owner_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)::int AS bridge_count,
                   COALESCE(SUM(input_tokens + output_tokens), 0)::int AS bridge_tokens,
                   COALESCE(SUM(cost_usd), 0)::float AS bridge_cost_usd
            FROM {TABLE_RESEARCH_COPILOT_BRIDGE_EVENT}
            WHERE owner_id = %s
              AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
            """,
            (owner_id,),
        )
        row = cur.fetchone()
    if not row:
        return {"bridge_count_today": 0, "bridge_tokens_today": 0, "bridge_cost_usd_today": 0.0}
    if isinstance(row, dict):
        return {
            "bridge_count_today": int(row.get("bridge_count") or 0),
            "bridge_tokens_today": int(row.get("bridge_tokens") or 0),
            "bridge_cost_usd_today": float(row.get("bridge_cost_usd") or 0.0),
        }
    return {
        "bridge_count_today": int(row[0] or 0),
        "bridge_tokens_today": int(row[1] or 0),
        "bridge_cost_usd_today": float(row[2] or 0.0),
    }


def list_recent_bridge_cases(
    conn: _Connection,
    *,
    owner_id: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Cases saved from bridge feedback loop (EX3)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, owner_id, trade_ref, outcome, lessons_md, tags, created_at
            FROM research.playbook_case
            WHERE owner_id = %s
              AND trade_ref->>'source' = 'bridge_feedback'
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (owner_id, limit),
        )
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            item = dict(r)
        else:
            item = {
                "id": r[0],
                "owner_id": r[1],
                "trade_ref": r[2],
                "outcome": r[3],
                "lessons_md": r[4],
                "tags": r[5],
                "created_at": r[6],
            }
        item["id"] = str(item["id"])
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = item["created_at"].isoformat()
        out.append(item)
    return out


__all__ = ["insert_event", "list_recent_bridge_cases", "usage_stats_today"]
