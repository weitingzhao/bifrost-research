"""Multi-agent graph — Triage + specialists (Wave RS-F3)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from agents import Agent, handoff
from agents.mcp import MCPServerSse

from bifrost_research.copilot.guardrails import (
    build_input_guardrail,
    build_output_guardrail,
)
from bifrost_research.copilot.models import resolve_model_for_agent
from bifrost_research.mcp.tools._write_common import WRITE_TOOL_NAMES

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


def _prefix_filter(prefixes: tuple[str, ...]):
    def _fn(_ctx: Any, tool: Any) -> bool:
        name = getattr(tool, "name", "") or ""
        return any(name.startswith(p) for p in prefixes)

    return _fn


def _write_filter(_ctx: Any, tool: Any) -> bool:
    name = getattr(tool, "name", "") or ""
    return name in WRITE_TOOL_NAMES


@lru_cache(maxsize=32)
def _mcp_server(*, cache_key: str, filter_key: str, prefixes: tuple[str, ...] = ()) -> MCPServerSse:
    del cache_key
    tool_filter = None
    if prefixes:
        tool_filter = _prefix_filter(prefixes)
    elif filter_key == "write":
        tool_filter = _write_filter
    return MCPServerSse(
        params={"url": _mcp_url()},
        cache_tools_list=True,
        name=f"research-mcp-{filter_key or 'all'}",
        client_session_timeout_seconds=30.0,
        tool_filter=tool_filter,
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
            "include_server_in_tool_names": False,
        },
    }
    if mcp is not None:
        kwargs["mcp_servers"] = [mcp]
    if handoffs_list:
        kwargs["handoffs"] = handoffs_list
    if tools:
        kwargs["tools"] = tools
    return Agent(**kwargs)


def build_discovery_agent(model_id: str) -> Agent[Any]:
    mcp = _mcp_server(cache_key=model_id, filter_key="discovery", prefixes=("research.discovery.",))
    return _agent(
        name="discovery",
        instructions=_read_instruction(
            "discovery",
            "You specialize in SEPA, Event Radar, Momentum, and discovery tools.",
        ),
        model_id=model_id,
        mcp=mcp,
    )


def build_analyze_agent(model_id: str) -> Agent[Any]:
    mcp = _mcp_server(
        cache_key=model_id,
        filter_key="analyze",
        prefixes=("research.vrp.", "research.vol_surface.", "research.opex_cycle."),
    )
    return _agent(
        name="analyze",
        instructions=_read_instruction(
            "analyze",
            "You specialize in VRP, vol surface, OpEx cycle, GEX, and flow analytics.",
        ),
        model_id=model_id,
        mcp=mcp,
    )


def build_validate_agent(model_id: str) -> Agent[Any]:
    mcp = _mcp_server(cache_key=model_id, filter_key="validate", prefixes=("research.backtest.",))
    return _agent(
        name="validate",
        instructions=_read_instruction(
            "validate",
            "You specialize in backtest runs, regime stats, and walk-forward validation.",
        ),
        model_id=model_id,
        mcp=mcp,
    )


def build_write_agent(model_id: str) -> Agent[Any]:
    mcp = _mcp_server(cache_key=model_id, filter_key="write")
    return _agent(
        name="write",
        instructions=_read_instruction(
            "write",
            "You handle hypothesis create/patch/retire and backtest writes. Always dry_run=true.",
        ),
        model_id=model_id,
        mcp=mcp,
    )


def build_explain_agent(model_id: str) -> Agent[Any]:
    return _agent(
        name="explain",
        instructions=_read_instruction(
            "explain",
            "You explain research concepts, glossary terms, and link to the Runbook. No MCP tools.",
        ),
        model_id=model_id,
    )


def build_verdict_agent(model_id: str) -> Agent[Any]:
    discovery = build_discovery_agent(model_id)
    analyze = build_analyze_agent(model_id)
    validate = build_validate_agent(model_id)
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


def build_triage_agent(model_id: str) -> Agent[Any]:
    discovery = build_discovery_agent(model_id)
    analyze = build_analyze_agent(model_id)
    validate = build_validate_agent(model_id)
    write = build_write_agent(model_id)
    explain = build_explain_agent(model_id)
    verdict = build_verdict_agent(model_id)

    mcp = _mcp_server(cache_key=model_id, filter_key="all")
    return _agent(
        name="triage",
        instructions=_read_instruction(
            "triage",
            f"{_SYSTEM_BASE}\n\nRoute the user to the best specialist via handoff. "
            "Discovery for SEPA/events; Analyze for VRP/vol; Validate for backtests; "
            "Write for hypothesis/backtest mutations; Explain for concepts; "
            "Verdict for compose/synthesis questions.",
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
        ],
    )


__all__ = [
    "build_analyze_agent",
    "build_discovery_agent",
    "build_explain_agent",
    "build_triage_agent",
    "build_validate_agent",
    "build_verdict_agent",
    "build_write_agent",
]
