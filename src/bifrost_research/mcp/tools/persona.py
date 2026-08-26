"""MCP read tools for agent personas (Wave RS-PS2)."""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.copilot.agents.agent_catalog import AGENT_NAMES
from bifrost_research.copilot.agents.persona_preferences import preferences_for_agent, parse_preferences
from bifrost_research.mcp.tools._common import with_conn
from bifrost_research.repositories import agent_persona as persona_repo


def _owner_id() -> str:
    return os.environ.get("RESEARCH_DEFAULT_OWNER", "owner").strip() or "owner"


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.persona.get_effective_preferences",
        description="**Read-only**. Does not modify data. Return owner-scoped persona preferences for all 8 agents.",
    )
    def get_effective_preferences() -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            owner = _owner_id()
            rows = persona_repo.list_for_owner(conn, owner)
            agents: dict[str, Any] = {}
            for row in rows:
                name = row.get("agent_name", "")
                prefs = parse_preferences(row.get("preferences_json"))
                agents[name] = {
                    "persona_md": row.get("persona_md"),
                    "guardrail_locked": row.get("guardrail_locked"),
                    "updated_at": row.get("updated_at"),
                    "preferences": preferences_for_agent(name, prefs),
                    "preferences_full": prefs.model_dump(),
                }
            return {
                "ok": True,
                "owner_id": owner,
                "agents": agents,
                "agent_names": list(AGENT_NAMES),
            }

        return with_conn(_run)
