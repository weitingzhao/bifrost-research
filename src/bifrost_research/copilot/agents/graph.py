"""Multi-agent graph — Triage + specialists (Wave RS-F3 + RS-PS persona overlay)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agents import Agent, handoff
from agents.mcp import MCPServerSse

from bifrost_research.copilot.agents.persona_overlay import assemble_instruction
from bifrost_research.copilot.guardrails import (
    build_input_guardrail,
    build_output_guardrail,
)
from bifrost_research.copilot.models import resolve_model_for_agent
from bifrost_research.db.conn import connect
from bifrost_research.repositories import agent_persona as persona_repo
from bifrost_research.repositories import playbook as playbook_repo

_INSTRUCTIONS_DIR = Path(__file__).resolve().parent / "instructions"

_SYSTEM_BASE = (
    "You are Bifrost Research Copilot. Answer using Research MCP tools when needed. "
    "For write actions, call write tools with dry_run=true to produce a diff preview. "
    "Cite tool names and symbols so the UI can link to Lab pages. "
    "D10: never suggest live order placement or daemon control."
)

_INSTRUCTION_FALLBACKS: dict[str, str] = {
    "discovery": (
        "You specialize in SEPA, Event Radar, Momentum, and discovery tools. "
        "Prefer research.discovery.* tools."
    ),
    "analyze": (
        "You specialize in VRP, vol surface, OpEx cycle, GEX, and flow analytics. "
        "Prefer research.vrp.*, research.vol_surface.*, research.opex_cycle.* tools."
    ),
    "validate": (
        "You specialize in backtest runs, regime stats, and walk-forward validation. "
        "Prefer research.backtest.* tools."
    ),
    "write": "You handle hypothesis create/patch/retire and backtest writes. Always dry_run=true.",
    "explain": "You explain research concepts, glossary terms, and link to the Runbook. No MCP tools.",
    "portfolio": (
        "You combine live portfolio holdings (trade.portfolio.snapshot / "
        "trade.market.quotes / trade.trading.recent_executions) with Research "
        "analytics to answer holdings-aware questions. Never suggest live orders."
    ),
    "curator": "Consolidate chats and hypotheses into playbook rule/note drafts via propose tools.",
    "verdict": (
        "Compose morning brief / EOD verdict by calling discovery, analyze, and validate "
        "specialists as tools, then synthesize a concise verdict."
    ),
    "triage": (
        f"{_SYSTEM_BASE}\n\nRoute the user to the best specialist via handoff. "
        "Discovery for SEPA/events; Analyze for VRP/vol; Validate for backtests; "
        "Write for hypothesis/backtest mutations; Explain for concepts; "
        "Verdict for compose/synthesis questions; "
        "Portfolio for questions about the user's actual holdings, positions, "
        "recent trades, or 'given my portfolio and current market' advice; "
        "Curator for consolidating learnings into playbook drafts."
    ),
}


def read_base_instruction(name: str) -> str:
    """Public: mirror image instruction file (no persona overlay)."""
    return _read_instruction(name, _INSTRUCTION_FALLBACKS.get(name, _SYSTEM_BASE))


def _read_instruction(name: str, fallback: str) -> str:
    path = _INSTRUCTIONS_DIR / f"{name}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return fallback


def _playbook_hard_blob(owner_id: str | None, agent_name: str) -> str | None:
    if not owner_id:
        return None
    try:
        conn = connect()
        try:
            rules = playbook_repo.list_rules_for_agent(
                conn,
                owner_id=owner_id,
                agent_name=agent_name,
                limit=12,
            )
        finally:
            conn.close()
    except Exception:
        return None
    return playbook_repo.format_hard_constraints_blob(rules)


def _assembled_instruction(agent_name: str, owner_id: str | None) -> str:
    base = read_base_instruction(agent_name)
    if not owner_id:
        return base
    try:
        conn = connect()
        try:
            persona_repo.seed_defaults_if_missing(conn, owner_id)
            row = persona_repo.get(conn, owner_id, agent_name)
        finally:
            conn.close()
    except Exception:
        return base
    if not row:
        return base
    hard = _playbook_hard_blob(owner_id, agent_name)
    return assemble_instruction(
        base,
        agent_name,
        str(row.get("persona_md") or ""),
        row.get("preferences_json") if isinstance(row.get("preferences_json"), dict) else {},
        playbook_hard_constraints=hard,
    )


def _mcp_url() -> str:
    return os.environ.get("RESEARCH_MCP_SSE_URL", "http://127.0.0.1:8796/sse")


def _new_mcp_server() -> MCPServerSse:
    return MCPServerSse(
        params={"url": _mcp_url()},
        cache_tools_list=True,
        name="research-mcp",
        client_session_timeout_seconds=30.0,
    )


def _guardrails() -> tuple[list[Any], list[Any]]:
    return [build_input_guardrail()], [build_output_guardrail()]


def _agent(
    *,
    name: str,
    instructions: str,
    model_id: str,
    mcp: MCPServerSse | None = None,
    handoffs_list: list[Any] | None = None,
    tools: list[Any] | None = None,
) -> Agent[Any]:
    in_g, out_g = _guardrails()
    kwargs: dict[str, Any] = {
        "name": name,
        "instructions": instructions,
        "model": resolve_model_for_agent(model_id),
        "input_guardrails": in_g,
        "output_guardrails": out_g,
        "mcp_config": {
            "convert_schemas_to_strict": True,
            "include_server_in_tool_names": True,
        },
    }
    if mcp is not None:
        kwargs["mcp_servers"] = [mcp]
    if handoffs_list:
        kwargs["handoffs"] = handoffs_list
    if tools:
        kwargs["tools"] = tools
    return Agent(**kwargs)


def build_discovery_agent(
    model_id: str,
    mcp: MCPServerSse | None = None,
    owner_id: str | None = None,
) -> Agent[Any]:
    return _agent(
        name="discovery",
        instructions=_assembled_instruction("discovery", owner_id),
        model_id=model_id,
        mcp=mcp,
    )


def build_analyze_agent(
    model_id: str,
    mcp: MCPServerSse | None = None,
    owner_id: str | None = None,
) -> Agent[Any]:
    return _agent(
        name="analyze",
        instructions=_assembled_instruction("analyze", owner_id),
        model_id=model_id,
        mcp=mcp,
    )


def build_validate_agent(
    model_id: str,
    mcp: MCPServerSse | None = None,
    owner_id: str | None = None,
) -> Agent[Any]:
    return _agent(
        name="validate",
        instructions=_assembled_instruction("validate", owner_id),
        model_id=model_id,
        mcp=mcp,
    )


def build_write_agent(
    model_id: str,
    mcp: MCPServerSse | None = None,
    owner_id: str | None = None,
) -> Agent[Any]:
    return _agent(
        name="write",
        instructions=_assembled_instruction("write", owner_id),
        model_id=model_id,
        mcp=mcp,
    )


def build_explain_agent(
    model_id: str,
    mcp: MCPServerSse | None = None,
    owner_id: str | None = None,
) -> Agent[Any]:
    del mcp
    return _agent(
        name="explain",
        instructions=_assembled_instruction("explain", owner_id),
        model_id=model_id,
    )


def build_portfolio_agent(
    model_id: str,
    mcp: MCPServerSse | None = None,
    owner_id: str | None = None,
) -> Agent[Any]:
    return _agent(
        name="portfolio",
        instructions=_assembled_instruction("portfolio", owner_id),
        model_id=model_id,
        mcp=mcp,
    )


def build_curator_agent(
    model_id: str,
    mcp: MCPServerSse | None = None,
    owner_id: str | None = None,
) -> Agent[Any]:
    return _agent(
        name="curator",
        instructions=_assembled_instruction("curator", owner_id),
        model_id=model_id,
        mcp=mcp,
    )


def build_loop_curator_agent(
    model_id: str,
    mcp: MCPServerSse | None = None,
    owner_id: str | None = None,
) -> Agent[Any]:
    return _agent(
        name="loop_curator",
        instructions=_assembled_instruction("loop_curator", owner_id),
        model_id=model_id,
        mcp=mcp,
    )


def build_verdict_agent(
    model_id: str,
    mcp: MCPServerSse | None = None,
    owner_id: str | None = None,
) -> Agent[Any]:
    discovery = build_discovery_agent(model_id, mcp=mcp, owner_id=owner_id)
    analyze = build_analyze_agent(model_id, mcp=mcp, owner_id=owner_id)
    validate = build_validate_agent(model_id, mcp=mcp, owner_id=owner_id)
    tools = [
        discovery.as_tool(tool_name="discovery_specialist", tool_description="SEPA/discovery data"),
        analyze.as_tool(tool_name="analyze_specialist", tool_description="VRP/vol/OpEx analytics"),
        validate.as_tool(tool_name="validate_specialist", tool_description="Backtest validation"),
    ]
    return _agent(
        name="verdict",
        instructions=_assembled_instruction("verdict", owner_id),
        model_id=model_id,
        tools=tools,
    )


def build_triage_agent(
    model_id: str,
    mcp: MCPServerSse | None = None,
    owner_id: str | None = None,
) -> Agent[Any]:
    discovery = build_discovery_agent(model_id, mcp=mcp, owner_id=owner_id)
    analyze = build_analyze_agent(model_id, mcp=mcp, owner_id=owner_id)
    validate = build_validate_agent(model_id, mcp=mcp, owner_id=owner_id)
    write = build_write_agent(model_id, mcp=mcp, owner_id=owner_id)
    explain = build_explain_agent(model_id, owner_id=owner_id)
    verdict = build_verdict_agent(model_id, mcp=mcp, owner_id=owner_id)
    portfolio = build_portfolio_agent(model_id, mcp=mcp, owner_id=owner_id)
    curator = build_curator_agent(model_id, mcp=mcp, owner_id=owner_id)
    loop_curator = build_loop_curator_agent(model_id, mcp=mcp, owner_id=owner_id)

    return _agent(
        name="triage",
        instructions=_assembled_instruction("triage", owner_id),
        model_id=model_id,
        mcp=mcp,
        handoffs_list=[
            handoff(discovery, tool_description_override="Route to Discovery specialist"),
            handoff(analyze, tool_description_override="Route to Analyze specialist"),
            handoff(validate, tool_description_override="Route to Validate specialist"),
            handoff(write, tool_description_override="Route to Write specialist"),
            handoff(explain, tool_description_override="Route to Explain specialist"),
            handoff(verdict, tool_description_override="Route to Verdict compose specialist"),
            handoff(portfolio, tool_description_override="Route to Portfolio specialist"),
            handoff(curator, tool_description_override="Route to Curator playbook specialist"),
            handoff(
                loop_curator,
                tool_description_override=(
                    "Route to Loop Curator for candidate→hypothesis→decision workflow"
                ),
            ),
        ],
    )


@asynccontextmanager
async def triage_agent_with_mcp(
    model_id: str,
    owner_id: str | None = None,
) -> AsyncIterator[Agent[Any]]:
    """Build triage agent with one connected MCP SSE session for the turn."""
    server = _new_mcp_server()
    try:
        await server.connect()
        yield build_triage_agent(model_id, mcp=server, owner_id=owner_id)
    finally:
        try:
            await server.cleanup()
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "build_analyze_agent",
    "build_curator_agent",
    "build_discovery_agent",
    "build_explain_agent",
    "build_loop_curator_agent",
    "build_portfolio_agent",
    "build_triage_agent",
    "build_validate_agent",
    "build_verdict_agent",
    "build_write_agent",
    "read_base_instruction",
    "triage_agent_with_mcp",
]
