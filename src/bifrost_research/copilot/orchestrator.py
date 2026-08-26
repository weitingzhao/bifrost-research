"""Copilot orchestrator — SDK adapter + legacy test path (Wave RS-F1.2).

Chat path uses openai-agents via ``agent_runtime``. Tests inject ``provider`` to
use the legacy hand-rolled loop in ``orchestrator_legacy``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from bifrost_research.copilot.agent_runtime import stream_agent
from bifrost_research.copilot.orchestrator_legacy import (
    execute_approved_write as _legacy_execute,
)
from bifrost_research.copilot.orchestrator_legacy import (
    orchestrate as _legacy_orchestrate,
)
from bifrost_research.copilot.providers import LlmProvider


async def orchestrate(
    *,
    messages: list[dict[str, Any]],
    model: str,
    max_tools: int = 8,
    session_id: str | None = None,
    owner_id: str | None = None,
    provider: LlmProvider | None = None,
    mcp: Any | None = None,
    turn_buffer: list[dict[str, Any]] | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames: token | tool_call | tool_result | error | done | agent_handoff."""
    if provider is not None:
        async for frame in _legacy_orchestrate(
            messages=messages,
            model=model,
            max_tools=max_tools,
            session_id=session_id,
            provider=provider,
            mcp=mcp,
        ):
            yield frame
        return

    async for frame in stream_agent(
        messages=messages,
        model_id=model,
        session_id=session_id,
        owner_id=owner_id,
        max_turns=max(1, max_tools) + 1,
        turn_buffer=turn_buffer,
    ):
        yield frame


async def execute_approved_write(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    approval_token: str,
    mcp: Any | None = None,
) -> dict[str, Any]:
    return await _legacy_execute(
        tool_name=tool_name,
        arguments=arguments,
        approval_token=approval_token,
        mcp=mcp,
    )


__all__ = ["execute_approved_write", "orchestrate"]
