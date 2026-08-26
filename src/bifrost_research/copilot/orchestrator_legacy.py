"""Copilot orchestrator — LLM loop + MCP tool dispatch (Wave RS-E2 / RS-E4).

Chat path exposes write tools but **forces dry_run=true** so the FE can show a
DiffApprovalCard. Actual writes go through ``POST /research/copilot/execute``
with a short-lived approval token (D-RS-E-e).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from bifrost_research.copilot.providers import (
    LlmProvider,
    ProviderTurn,
    ToolCallRequest,
    ToolSpec,
    resolve_provider,
)
from bifrost_research.copilot.rate_limit import record_usage
from bifrost_research.mcp.server import ALL_TOOL_NAMES, TOOL_NAMES, create_mcp_server
from bifrost_research.mcp.tools._write_common import WRITE_TOOL_NAMES

logger = logging.getLogger("bifrost.copilot.audit")

_SYSTEM = (
    "You are Bifrost Research Copilot. Answer using Research MCP tools when needed. "
    "For write actions (create/patch/retire hypothesis, run backtest), call the write "
    "tool with dry_run=true to produce a diff preview — never pass dry_run=false from chat. "
    "Cite tool names and symbols so the UI can link to Lab pages. "
    "D10: never suggest live order placement or daemon control."
)


def _is_write_tool(name: str) -> bool:
    return name in WRITE_TOOL_NAMES or any(
        x in name
        for x in (".create", ".patch", ".retire", ".write", ".delete", ".run_event")
    )


def _tool_specs(mcp: Any, *, include_writes: bool = True) -> list[ToolSpec]:
    tools = mcp._tool_manager.list_tools()  # noqa: SLF001
    allowed = set(ALL_TOOL_NAMES if include_writes else TOOL_NAMES)
    specs: list[ToolSpec] = []
    for t in tools:
        if t.name not in allowed:
            continue
        specs.append(
            ToolSpec(
                name=t.name,
                description=t.description or "",
                input_schema=t.parameters or {"type": "object", "properties": {}},
            )
        )
    return specs


async def _dispatch_tool(
    mcp: Any,
    call: ToolCallRequest,
    *,
    force_write_dry_run: bool = True,
) -> dict[str, Any]:
    name = call.name
    arguments = dict(call.arguments or {})

    if name not in ALL_TOOL_NAMES and name not in TOOL_NAMES:
        # Allow unknown only if registered on the server (tests may inject)
        pass

    if _is_write_tool(name) and force_write_dry_run:
        arguments["dry_run"] = True
        arguments.pop("approval_token", None)

    try:
        result = await mcp.call_tool(name, arguments)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if isinstance(result, dict):
        return result
    texts: list[str] = []
    for block in result or []:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    joined = "\n".join(texts) if texts else "[]"
    try:
        parsed = json.loads(joined)
        if isinstance(parsed, dict):
            return parsed
        return {"ok": True, "data": parsed}
    except json.JSONDecodeError:
        return {"ok": True, "data": joined}


def _sse(event: str, payload: dict[str, Any]) -> str:
    body = json.dumps({"event": event, **payload}, default=str)
    return f"data: {body}\n\n"


async def orchestrate(
    *,
    messages: list[dict[str, Any]],
    model: str,
    max_tools: int = 8,
    session_id: str | None = None,
    provider: LlmProvider | None = None,
    mcp: Any | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames: token | tool_call | tool_result | error | done."""
    llm = provider or resolve_provider(model)
    server = mcp or create_mcp_server()
    tools = _tool_specs(server, include_writes=True)

    history: list[dict[str, Any]] = [{"role": "system", "content": _SYSTEM}]
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant", "system", "tool"):
            history.append(dict(msg))
        else:
            history.append({"role": "user", "content": str(content)})

    tools_used = 0
    total_tokens = 0
    total_cost = 0.0

    for _round in range(max(1, max_tools) + 1):
        turn: ProviderTurn = llm.complete(messages=history, tools=tools, model=model)
        total_tokens += turn.input_tokens + turn.output_tokens
        total_cost += turn.cost_usd

        if turn.error:
            yield _sse("error", {"message": turn.error, "session_id": session_id})
            record_usage(tokens=total_tokens, cost_usd=total_cost)
            yield _sse("done", {"session_id": session_id, "ok": False})
            return

        if turn.text:
            yield _sse("token", {"text": turn.text, "session_id": session_id})

        if not turn.tool_calls:
            record_usage(tokens=total_tokens, cost_usd=total_cost)
            logger.info(
                "copilot_turn session=%s model=%s tokens=%s cost=%.6f tools=%s",
                session_id,
                model,
                total_tokens,
                total_cost,
                tools_used,
            )
            yield _sse(
                "done",
                {
                    "session_id": session_id,
                    "ok": True,
                    "tokens": total_tokens,
                    "cost_usd": round(total_cost, 6),
                },
            )
            return

        if turn.assistant_payload is not None:
            history.append(
                {
                    "role": "assistant",
                    "content": turn.text,
                    "assistant_payload": turn.assistant_payload,
                }
            )
        else:
            history.append({"role": "assistant", "content": turn.text})

        for call in turn.tool_calls:
            if tools_used >= max_tools:
                yield _sse(
                    "error",
                    {
                        "message": f"max_tools={max_tools} reached",
                        "session_id": session_id,
                    },
                )
                break
            tools_used += 1
            # Surface forced dry_run args to FE for write tools
            display_args = dict(call.arguments or {})
            if _is_write_tool(call.name):
                display_args["dry_run"] = True
                display_args.pop("approval_token", None)
            yield _sse(
                "tool_call",
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": display_args,
                    "session_id": session_id,
                },
            )
            result = await _dispatch_tool(server, call, force_write_dry_run=True)
            yield _sse(
                "tool_result",
                {
                    "id": call.id,
                    "name": call.name,
                    "result": result,
                    "session_id": session_id,
                },
            )
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": json.dumps(result, default=str),
                }
            )
        else:
            continue
        break

    record_usage(tokens=total_tokens, cost_usd=total_cost)
    yield _sse("done", {"session_id": session_id, "ok": True, "tokens": total_tokens})


async def execute_approved_write(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    approval_token: str,
    mcp: Any | None = None,
) -> dict[str, Any]:
    """Run a write tool with dry_run=false after token validation (inside tool)."""
    if tool_name not in WRITE_TOOL_NAMES:
        return {"ok": False, "error": f"not a write tool: {tool_name}", "status": 400}
    server = mcp or create_mcp_server()
    args = dict(arguments or {})
    args["dry_run"] = False
    args["approval_token"] = approval_token
    call = ToolCallRequest(id="exec", name=tool_name, arguments=args)
    return await _dispatch_tool(server, call, force_write_dry_run=False)


# For tests: sync helper wrapping a fake provider that returns canned turns
ProviderFactory = Callable[[], LlmProvider]
