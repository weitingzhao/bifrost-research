"""openai-agents SDK runtime — SSE adapter (Wave RS-F1.2+)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from agents import Runner
from agents.exceptions import (
    InputGuardrailTripwireTriggered,
    OutputGuardrailTripwireTriggered,
)
from agents.items import HandoffCallItem, ItemHelpers, ToolCallItem, ToolCallOutputItem
from agents.stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
)

from bifrost_research.copilot.agents.graph import triage_agent_with_mcp
from bifrost_research.copilot.guardrails import (
    D10_FREEZE_CODE,
    D10_FREEZE_MESSAGE,
    check_input,
)
from bifrost_research.copilot.models import ModelConfigError
from bifrost_research.copilot.providers import estimate_cost
from bifrost_research.copilot.rate_limit import record_usage
from bifrost_research.copilot.tracing import maybe_configure_otlp, trace_event
from bifrost_research.copilot.write_gate import force_chat_dry_run
from bifrost_research.db.conn import connect
from bifrost_research.mcp.server import ALL_TOOL_NAMES
from bifrost_research.repositories.ai_action_log import log_guardrail_rejection

logger = logging.getLogger("bifrost.copilot.audit")

maybe_configure_otlp()

_KNOWN_TOOL_BY_SAFE = {n.replace(".", "_"): n for n in ALL_TOOL_NAMES}


def _canonical_tool_name(name: str) -> str:
    """Map SDK-prefixed / underscore tool names back to research.* MCP names for FE chips."""
    raw = (name or "").strip()
    if raw in ALL_TOOL_NAMES:
        return raw
    if raw in _KNOWN_TOOL_BY_SAFE:
        return _KNOWN_TOOL_BY_SAFE[raw]
    # mcp_<server>__research_hypothesis_list
    if "__" in raw:
        tail = raw.rsplit("__", 1)[-1]
        if tail in _KNOWN_TOOL_BY_SAFE:
            return _KNOWN_TOOL_BY_SAFE[tail]
        if tail in ALL_TOOL_NAMES:
            return tail
    return raw


def _audit_guardrail_best_effort(
    *,
    session_id: str | None,
    model_id: str,
    phase: str,
    matched_pattern: str | None = None,
) -> None:
    try:
        conn = connect()
        try:
            log_guardrail_rejection(
                conn,
                reason=f"{D10_FREEZE_CODE}:{phase}",
                session_id=session_id,
                model=model_id,
                matched_pattern=matched_pattern,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.debug("guardrail audit skipped session=%s", session_id, exc_info=True)


def _sse(event: str, payload: dict[str, Any]) -> str:
    body = json.dumps({"event": event, **payload}, default=str)
    return f"data: {body}\n\n"


def _user_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        if msg.get("role") == "user":
            parts.append(str(msg.get("content", "")))
    return "\n".join(parts)


def _sdk_input(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for msg in messages:
        role = msg.get("role", "user")
        if role in ("user", "assistant", "system"):
            out.append({"role": role, "content": str(msg.get("content", ""))})
    return out or [{"role": "user", "content": "Hello"}]


def _extract_tool_call(item: ToolCallItem) -> tuple[str, str, dict[str, Any]]:
    raw = item.raw_item
    call_id = getattr(raw, "call_id", None) or getattr(raw, "id", None) or "tool"
    name = getattr(raw, "name", None) or getattr(item, "name", None) or "unknown"
    args_raw = getattr(raw, "arguments", None) or "{}"
    if isinstance(args_raw, dict):
        args = dict(args_raw)
    else:
        try:
            args = json.loads(str(args_raw) or "{}")
        except json.JSONDecodeError:
            args = {}
    return str(call_id), _canonical_tool_name(str(name)), args


def _resolve_output_call_id(item: ToolCallOutputItem) -> str | None:
    """Best-effort resolve the tool_call_id for a ToolCallOutputItem.

    openai-agents SDK stores this on ``raw_item.call_id`` for the function-tool
    path, but MCP hosted tools sometimes surface it under ``id`` / ``tool_call_id``
    or as a plain dict.  The FE matches ``tool_result.id → tool_call.id`` — if the
    resolved id is missing, the UI card stays PENDING forever.
    """
    raw = item.raw_item
    for attr in ("call_id", "id", "tool_call_id"):
        v = getattr(raw, attr, None)
        if v:
            return str(v)
    if isinstance(raw, dict):
        for key in ("call_id", "id", "tool_call_id"):
            v = raw.get(key)
            if v:
                return str(v)
    for attr in ("call_id", "id"):
        v = getattr(item, attr, None)
        if v:
            return str(v)
    return None


def _extract_tool_result(item: ToolCallOutputItem) -> Any:
    """Unwrap MCP tool output into a plain ``{ok, data|error, ...}`` envelope.

    MCP servers return content parts like ``{"type": "text", "text": "<json>"}`` and
    the SDK forwards that verbatim.  If we don't parse the inner JSON here, the FE
    renders the raw envelope and cannot detect success/failure from ``ok``.
    """
    output = item.output
    if isinstance(output, str):
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {"ok": True, "data": output}
    if isinstance(output, dict):
        if output.get("type") == "text" and isinstance(output.get("text"), str):
            try:
                return json.loads(output["text"])
            except json.JSONDecodeError:
                return {"ok": True, "data": output["text"]}
        return output
    if isinstance(output, list):
        for part in output:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                try:
                    return json.loads(part["text"])
                except json.JSONDecodeError:
                    continue
        return {"ok": True, "data": output}
    return {"ok": True, "data": str(output)}


def _text_delta(event: RawResponsesStreamEvent) -> str:
    data = event.data
    typ = getattr(data, "type", None) or (data.get("type") if isinstance(data, dict) else None)
    if typ in ("response.output_text.delta", "response.text.delta"):
        delta = getattr(data, "delta", None)
        if delta is None and isinstance(data, dict):
            delta = data.get("delta")
        return str(delta or "")
    return ""


async def stream_agent(
    *,
    messages: list[dict[str, Any]],
    model_id: str,
    session_id: str | None = None,
    max_turns: int = 12,
) -> AsyncIterator[str]:
    """Yield SSE frames compatible with RS-E contract + RS-F extensions."""
    user_blob = _user_text(messages)
    guard = check_input(user_blob)
    if guard.tripwire:
        trace_event("guardrail", {"session_id": session_id, "phase": "input", "pattern": guard.matched_pattern})
        _audit_guardrail_best_effort(
            session_id=session_id,
            model_id=model_id,
            phase="input",
            matched_pattern=guard.matched_pattern,
        )
        yield _sse(
            "error",
            {"message": D10_FREEZE_MESSAGE, "code": D10_FREEZE_CODE, "session_id": session_id},
        )
        yield _sse("guardrail", {"phase": "input", "code": D10_FREEZE_CODE, "session_id": session_id})
        yield _sse("done", {"session_id": session_id, "ok": False})
        return

    current_agent = "triage"
    total_tokens = 0
    total_cost = 0.0
    ok = True

    try:
        async with triage_agent_with_mcp(model_id) as agent:
            try:
                result = Runner.run_streamed(
                    agent,
                    input=_sdk_input(messages),
                    max_turns=max_turns,
                )

                pending_calls: dict[str, tuple[str, dict[str, Any]]] = {}

                async for event in result.stream_events():
                    if isinstance(event, AgentUpdatedStreamEvent):
                        prev = current_agent
                        current_agent = event.new_agent.name or "agent"
                        if prev != current_agent:
                            yield _sse(
                                "agent_handoff",
                                {
                                    "from": prev,
                                    "to": current_agent,
                                    "reason": "agent_updated",
                                    "session_id": session_id,
                                },
                            )
                            trace_event(
                                "agent_handoff",
                                {"from": prev, "to": current_agent, "session_id": session_id},
                            )
                        continue

                    if isinstance(event, RawResponsesStreamEvent):
                        delta = _text_delta(event)
                        if delta:
                            yield _sse("token", {"text": delta, "session_id": session_id})
                        continue

                    if isinstance(event, RunItemStreamEvent):
                        item = event.item
                        if event.name == "handoff_requested" and isinstance(item, HandoffCallItem):
                            raw = item.raw_item
                            target = getattr(raw, "name", None) or "specialist"
                            yield _sse(
                                "agent_handoff",
                                {
                                    "from": current_agent,
                                    "to": str(target),
                                    "reason": "handoff_requested",
                                    "session_id": session_id,
                                },
                            )
                            continue

                        if event.name == "tool_called" and isinstance(item, ToolCallItem):
                            call_id, name, args = _extract_tool_call(item)
                            display_args = force_chat_dry_run(name, args)
                            pending_calls[call_id] = (name, display_args)
                            yield _sse(
                                "tool_call",
                                {
                                    "id": call_id,
                                    "name": name,
                                    "arguments": display_args,
                                    "session_id": session_id,
                                },
                            )
                            trace_event(
                                "tool_call",
                                {"id": call_id, "name": name, "session_id": session_id},
                            )
                            continue

                        if event.name == "tool_output" and isinstance(item, ToolCallOutputItem):
                            resolved = _resolve_output_call_id(item)
                            # FIFO fallback — SDK emits tool_output events in call order.
                            # Without this, if raw.call_id is missing we'd emit a fixed
                            # sentinel id ("tool") that never matches any pending FE chip
                            # and every tool card stays PENDING forever.
                            if resolved is None or resolved not in pending_calls:
                                resolved = next(iter(pending_calls), None) or "tool"
                            name, _ = pending_calls.pop(resolved, ("unknown", {}))
                            result_payload = _extract_tool_result(item)
                            yield _sse(
                                "tool_result",
                                {
                                    "id": resolved,
                                    "name": name,
                                    "result": result_payload,
                                    "session_id": session_id,
                                },
                            )
                            trace_event(
                                "tool_result",
                                {"id": resolved, "name": name, "session_id": session_id},
                            )
                            continue

                        if event.name == "message_output_created":
                            text = ItemHelpers.extract_last_text([item]) or ItemHelpers.extract_last_content(
                                [item]
                            )
                            if isinstance(text, str) and text.strip():
                                yield _sse("token", {"text": text, "session_id": session_id})

                if result.run_loop_task is not None:
                    await result.run_loop_task
                usage = result.context_wrapper.usage
                in_tok = int(getattr(usage, "input_tokens", 0) or 0)
                out_tok = int(getattr(usage, "output_tokens", 0) or 0)
                total_tokens = in_tok + out_tok
                total_cost = estimate_cost(model_id, in_tok, out_tok)

            except InputGuardrailTripwireTriggered:
                ok = False
                trace_event("guardrail", {"session_id": session_id, "phase": "input_sdk"})
                _audit_guardrail_best_effort(
                    session_id=session_id, model_id=model_id, phase="input_sdk"
                )
                yield _sse(
                    "error",
                    {
                        "message": D10_FREEZE_MESSAGE,
                        "code": D10_FREEZE_CODE,
                        "session_id": session_id,
                    },
                )
                yield _sse(
                    "guardrail",
                    {"phase": "input", "code": D10_FREEZE_CODE, "session_id": session_id},
                )
            except OutputGuardrailTripwireTriggered:
                ok = False
                trace_event("guardrail", {"session_id": session_id, "phase": "output_sdk"})
                _audit_guardrail_best_effort(
                    session_id=session_id, model_id=model_id, phase="output_sdk"
                )
                yield _sse(
                    "error",
                    {
                        "message": D10_FREEZE_MESSAGE,
                        "code": D10_FREEZE_CODE,
                        "session_id": session_id,
                    },
                )
                yield _sse(
                    "guardrail",
                    {"phase": "output", "code": D10_FREEZE_CODE, "session_id": session_id},
                )
            except Exception as exc:
                ok = False
                logger.exception("copilot agent runtime error session=%s", session_id)
                yield _sse("error", {"message": str(exc), "session_id": session_id})
    except ModelConfigError as exc:
        yield _sse("error", {"message": str(exc), "session_id": session_id})
        yield _sse("done", {"session_id": session_id, "ok": False})
        return
    except Exception as exc:
        # Reached only when triage_agent_with_mcp.__aenter__() (i.e. MCPServerSse.connect())
        # fails or the agent graph build raises before the runtime loop starts.
        # The SDK's own MCP errors already read "Failed to connect to MCP server '<name>': ..."
        # so we surface the raw class + message rather than prefix an assumed root cause.
        ok = False
        logger.exception("copilot runtime setup error session=%s", session_id)
        exc_name = type(exc).__name__
        yield _sse(
            "error",
            {"message": f"{exc_name}: {exc}", "session_id": session_id},
        )

    record_usage(tokens=total_tokens, cost_usd=total_cost)
    logger.info(
        "copilot_turn session=%s model=%s tokens=%s cost=%.6f agent=%s",
        session_id,
        model_id,
        total_tokens,
        total_cost,
        current_agent,
    )
    yield _sse(
        "done",
        {
            "session_id": session_id,
            "ok": ok,
            "tokens": total_tokens,
            "cost_usd": round(total_cost, 6),
        },
    )


__all__ = ["stream_agent"]
