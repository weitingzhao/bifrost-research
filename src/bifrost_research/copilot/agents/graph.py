"""Multi-agent graph — Triage + specialists (Wave RS-F3).

Uses a **single** shared MCPServerSse per Copilot turn (multi-SSE sessions
crash FastMCP). Specialists share the same connected server; instructions
constrain which tools they should prefer.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agents import Agent, handoff
from agents.mcp import MCPServerSse

from bifrost_research.copilot.guardrails import (
    build_input_guardrail,
    build_output_guardrail,
)
from bifrost_research.copilot.models import resolve_model_for_agent

_INSTRUCTIONS_DIR = Path(__file__).resolve().parent / "instructions"

_SYSTEM_BASE = (
    "You are Bifrost Research Copilot. Answer using Research MCP tools when needed. "
    "For write actions, call write tools with dry_run=true to produce a diff preview. "
    "Cite tool names and symbols so the UI can link to Lab pages. "
    "D10: never suggest live order placement or daemon control."
)


def _read_instruction(name: str, fallback: str) -> str:
    path = _INSTRUCTIONS_DIR / f"{name}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return fallback


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
            # Sanitize MCP tool names for OpenAI-compatible providers (DeepSeek rejects dots).
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


def build_discovery_agent(model_id: str, mcp: MCPServerSse | None = None) -> Agent[Any]:
    return _agent(
        name="discovery",
        instructions=_read_instruction(
            "discovery",
            "You specialize in SEPA, Event Radar, Momentum, and discovery tools. "
            "Prefer research.discovery.* tools.",
        ),
        model_id=model_id,
        mcp=mcp,
    )


def build_analyze_agent(model_id: str, mcp: MCPServerSse | None = None) -> Agent[Any]:
    return _agent(
        name="analyze",
        instructions=_read_instruction(
            "analyze",
            "You specialize in VRP, vol surface, OpEx cycle, GEX, and flow analytics. "
            "Prefer research.vrp.*, research.vol_surface.*, research.opex_cycle.* tools.",
        ),
        model_id=model_id,
        mcp=mcp,
    )


def build_validate_agent(model_id: str, mcp: MCPServerSse | None = None) -> Agent[Any]:
    return _agent(
        name="validate",
        instructions=_read_instruction(
            "validate",
            "You specialize in backtest runs, regime stats, and walk-forward validation. "
            "Prefer research.backtest.* tools.",
        ),
        model_id=model_id,
        mcp=mcp,
    )


def build_write_agent(model_id: str, mcp: MCPServerSse | None = None) -> Agent[Any]:
    return _agent(
        name="write",
        instructions=_read_instruction(
            "write",
            "You handle hypothesis create/patch/retire and backtest writes. Always dry_run=true.",
        ),
        model_id=model_id,
        mcp=mcp,
    )


def build_explain_agent(model_id: str, mcp: MCPServerSse | None = None) -> Agent[Any]:
    del mcp  # explain has no MCP tools
    return _agent(
        name="explain",
        instructions=_read_instruction(
            "explain",
            "You explain research concepts, glossary terms, and link to the Runbook. No MCP tools.",
        ),
        model_id=model_id,
    )


def build_portfolio_agent(model_id: str, mcp: MCPServerSse | None = None) -> Agent[Any]:
    return _agent(
        name="portfolio",
        instructions=_read_instruction(
            "portfolio",
            "You combine live portfolio holdings (trade.portfolio.snapshot / "
            "trade.market.quotes / trade.trading.recent_executions) with Research "
            "analytics to answer holdings-aware questions. Never suggest live orders.",
        ),
        model_id=model_id,
        mcp=mcp,
    )


def build_curator_agent(model_id: str, mcp: MCPServerSse | None = None) -> Agent[Any]:
    """Curator — distill chats into playbook drafts (RS-KB4)."""
    return _agent(
        name="curator",
        instructions=_read_instruction(
            "curator",
            "Consolidate chats and hypotheses into playbook rule/note drafts via propose tools.",
        ),
        model_id=model_id,
        mcp=mcp,
    )


def build_verdict_agent(model_id: str, mcp: MCPServerSse | None = None) -> Agent[Any]:
    discovery = build_discovery_agent(model_id, mcp=mcp)
    analyze = build_analyze_agent(model_id, mcp=mcp)
    validate = build_validate_agent(model_id, mcp=mcp)
    tools = [
        discovery.as_tool(tool_name="discovery_specialist", tool_description="SEPA/discovery data"),
        analyze.as_tool(tool_name="analyze_specialist", tool_description="VRP/vol/OpEx analytics"),
        validate.as_tool(tool_name="validate_specialist", tool_description="Backtest validation"),
    ]
    return _agent(
        name="verdict",
        instructions=_read_instruction(
            "verdict",
            "Compose morning brief / EOD verdict by calling discovery, analyze, and validate "
            "specialists as tools, then synthesize a concise verdict.",
        ),
        model_id=model_id,
        tools=tools,
    )


def build_triage_agent(model_id: str, mcp: MCPServerSse | None = None) -> Agent[Any]:
    discovery = build_discovery_agent(model_id, mcp=mcp)
    analyze = build_analyze_agent(model_id, mcp=mcp)
    validate = build_validate_agent(model_id, mcp=mcp)
    write = build_write_agent(model_id, mcp=mcp)
    explain = build_explain_agent(model_id)
    verdict = build_verdict_agent(model_id, mcp=mcp)
    portfolio = build_portfolio_agent(model_id, mcp=mcp)
    curator = build_curator_agent(model_id, mcp=mcp)

    return _agent(
        name="triage",
        instructions=_read_instruction(
            "triage",
            f"{_SYSTEM_BASE}\n\nRoute the user to the best specialist via handoff. "
            "Discovery for SEPA/events; Analyze for VRP/vol; Validate for backtests; "
            "Write for hypothesis/backtest mutations; Explain for concepts; "
            "Verdict for compose/synthesis questions; "
            "Portfolio for questions about the user's actual holdings, positions, "
            "recent trades, or 'given my portfolio and current market' advice; "
            "Curator for consolidating learnings into playbook drafts.",
        ),
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
        ],
    )


@asynccontextmanager
async def triage_agent_with_mcp(model_id: str) -> AsyncIterator[Agent[Any]]:
    """Build triage agent with one connected MCP SSE session for the turn."""
    server = _new_mcp_server()
    try:
        await server.connect()
        yield build_triage_agent(model_id, mcp=server)
    finally:
        try:
            await server.cleanup()
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "build_analyze_agent",
    "build_discovery_agent",
    "build_explain_agent",
    "build_curator_agent",
    "build_portfolio_agent",
    "build_triage_agent",
    "build_validate_agent",
    "build_verdict_agent",
    "build_write_agent",
    "triage_agent_with_mcp",
]
