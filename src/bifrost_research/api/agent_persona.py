"""Agent persona REST API — Wave RS-PS1."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from bifrost_research.auth.deps import require_owner
from bifrost_research.copilot.agents.agent_catalog import AGENT_LABELS, AGENT_LABELS_ZH, AGENT_NAMES
from bifrost_research.copilot.agents.graph import read_base_instruction
from bifrost_research.copilot.agents.persona_overlay import assemble_instruction
from bifrost_research.db.conn import connect
from bifrost_research.repositories import agent_persona as persona_repo

router = APIRouter(prefix="/research/agent_persona", tags=["research-agent-persona"])


class PersonaPutBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    persona_md: str | None = None
    preferences_json: dict[str, Any] | None = None


def _assembled_preview(owner_id: str, agent_name: str, row: dict[str, Any]) -> str:
    base = read_base_instruction(agent_name)
    return assemble_instruction(
        base,
        agent_name,
        str(row.get("persona_md") or ""),
        row.get("preferences_json") if isinstance(row.get("preferences_json"), dict) else {},
    )


@router.get("")
def list_personas(owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    conn = connect()
    try:
        rows = persona_repo.list_for_owner(conn, owner_id)
        out = []
        for r in rows:
            name = r["agent_name"]
            out.append(
                {
                    **r,
                    "label": AGENT_LABELS.get(name, name),
                    "label_zh": AGENT_LABELS_ZH.get(name, name),
                    "base_instruction_preview": read_base_instruction(name)[:400],
                    "assembled_preview": _assembled_preview(owner_id, name, r)[:800],
                }
            )
        return {"ok": True, "agents": out, "agent_names": list(AGENT_NAMES)}
    finally:
        conn.close()


@router.get("/{agent_name}")
def get_persona(agent_name: str, owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    if agent_name not in AGENT_NAMES:
        raise HTTPException(status_code=404, detail="unknown agent")
    conn = connect()
    try:
        row = persona_repo.get(conn, owner_id, agent_name)
        if row is None:
            raise HTTPException(status_code=404, detail="persona not found")
        return {
            "ok": True,
            "persona": row,
            "label": AGENT_LABELS.get(agent_name, agent_name),
            "label_zh": AGENT_LABELS_ZH.get(agent_name, agent_name),
            "base_instruction": read_base_instruction(agent_name),
            "assembled_preview": _assembled_preview(owner_id, agent_name, row),
        }
    finally:
        conn.close()


@router.put("/{agent_name}")
def put_persona(
    agent_name: str,
    body: PersonaPutBody,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    if agent_name not in AGENT_NAMES:
        raise HTTPException(status_code=404, detail="unknown agent")
    conn = connect()
    try:
        existing = persona_repo.get(conn, owner_id, agent_name)
        if existing and existing.get("guardrail_locked"):
            # Allow preferences + additive persona text; core locked flag stays
            pass
        row = persona_repo.upsert(
            conn,
            owner_id,
            agent_name,
            persona_md=body.persona_md,
            preferences_json=body.preferences_json,
        )
        return {"ok": True, "persona": row}
    finally:
        conn.close()


@router.post("/{agent_name}/reset")
def reset_persona(agent_name: str, owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    if agent_name not in AGENT_NAMES:
        raise HTTPException(status_code=404, detail="unknown agent")
    conn = connect()
    try:
        row = persona_repo.reset(conn, owner_id, agent_name)
        return {"ok": True, "persona": row}
    finally:
        conn.close()
