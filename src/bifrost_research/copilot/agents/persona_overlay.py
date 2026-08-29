"""Assemble base instructions + owner persona overlay (Wave RS-PS)."""

from __future__ import annotations

import json
from typing import Any

from bifrost_research.copilot.agents.agent_catalog import (
    GUARDRAIL_LOCKED_AGENTS,
    VALIDATE_NEUTRAL_APPENDIX,
)
from bifrost_research.copilot.agents.persona_preferences import (
    parse_preferences,
    preferences_for_agent,
)


def render_persona_overlay(
    agent_name: str,
    persona_md: str,
    preferences_json: dict[str, Any] | None,
) -> str:
    """Build owner overlay block appended to base agent instructions."""
    parts: list[str] = []
    md = (persona_md or "").strip()
    if md:
        parts.append("Owner persona (free text):")
        parts.append(md)

    prefs = parse_preferences(preferences_json)
    filtered = preferences_for_agent(agent_name, prefs)
    if filtered:
        parts.append("Owner explicit preferences:")
        parts.append(json.dumps(filtered, ensure_ascii=False, indent=2))
        parts.append(
            "When your recommendation touches any preference slot above, cite the slot "
            "inline (e.g. aligned with favor_signals=breakout)."
        )
        if agent_name == "portfolio" and filtered.get("max_single_position_pct") is not None:
            parts.append(
                f"Portfolio mandate: compare each large position to "
                f"max_single_position_pct={filtered['max_single_position_pct']} and flag breaches."
            )

    if agent_name in GUARDRAIL_LOCKED_AGENTS:
        parts.append(VALIDATE_NEUTRAL_APPENDIX)

    return "\n".join(parts).strip()


def assemble_instruction(
    base: str,
    agent_name: str,
    persona_md: str,
    preferences_json: dict[str, Any] | None,
    playbook_hard_constraints: str | None = None,
) -> str:
    """Merge base instruction file, persona overlay, and optional hard playbook rules."""
    chunks = [base.strip()]
    if agent_name == "loop_curator":
        from bifrost_research.copilot.agents.loop_curator import loop_curator_appendix

        chunks.append(loop_curator_appendix())
    overlay = render_persona_overlay(agent_name, persona_md, preferences_json)
    if overlay:
        chunks.append("---")
        chunks.append(overlay)
    if playbook_hard_constraints:
        chunks.append("---")
        chunks.append(playbook_hard_constraints)
    return "\n\n".join(chunks).strip()


__all__ = ["assemble_instruction", "render_persona_overlay"]
