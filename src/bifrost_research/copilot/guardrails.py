"""D10 hard-reject guardrails for Research Copilot (Wave RS-F1.3 / F4.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agents import Agent, GuardrailFunctionOutput, InputGuardrail, OutputGuardrail
from agents.run_context import RunContextWrapper

D10_FREEZE_MESSAGE = "This action is blocked by D10 execution freeze policy."
D10_FREEZE_CODE = "D10_FREEZE"

# Compiled regex list — hard reject on match (input + output).
D10_FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bplace_order\b",
        r"\bib:operator:cmd\b",
        r"\bdaemon\.(start|stop|scale|resume)\b",
        r"\bclient_id\s*=\s*\d+\b",
        r"\bkubectl\s+(apply|delete|scale)\b",
        r"\bmake\s+(promote|deploy)\b",
    )
)

# Whitelist: literal quotes discussing forbidden terms in research context.
_SAFE_LITERAL_QUOTES = (
    '"place_order"',
    "'place_order'",
    "what is place_order",
    "explain place_order",
)


@dataclass
class GuardrailResult:
    tripwire: bool
    reason: str | None = None
    matched_pattern: str | None = None


def _is_whitelisted(text: str) -> bool:
    lower = text.lower()
    return any(q in lower for q in _SAFE_LITERAL_QUOTES)


def check_input(text: str) -> GuardrailResult:
    if _is_whitelisted(text):
        return GuardrailResult(tripwire=False)
    for pat in D10_FORBIDDEN_PATTERNS:
        if pat.search(text):
            return GuardrailResult(
                tripwire=True,
                reason=D10_FREEZE_MESSAGE,
                matched_pattern=pat.pattern,
            )
    return GuardrailResult(tripwire=False)


def check_output(text: str) -> GuardrailResult:
    result = check_input(text)
    if result.tripwire:
        return result
    # Structural: live trade recommendation language
    live_trade = re.search(
        r"\b(buy|sell|short)\s+\d+\s+(shares|contracts)\b.*\b(now|immediately|market order)\b",
        text,
        re.IGNORECASE,
    )
    if live_trade:
        return GuardrailResult(
            tripwire=True,
            reason=D10_FREEZE_MESSAGE,
            matched_pattern="live_trade_recommendation",
        )
    return GuardrailResult(tripwire=False)


def _collect_user_text(input_data: str | list[Any]) -> str:
    if isinstance(input_data, str):
        return input_data
    parts: list[str] = []
    for item in input_data:
        if isinstance(item, dict):
            content = item.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "input_text":
                        parts.append(str(block.get("text", "")))
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


async def _input_guardrail_fn(
    ctx: RunContextWrapper[Any],
    agent: Agent[Any],
    input_data: str | list[Any],
) -> GuardrailFunctionOutput:
    del ctx, agent
    text = _collect_user_text(input_data)
    result = check_input(text)
    return GuardrailFunctionOutput(
        output_info={"matched": result.matched_pattern},
        tripwire_triggered=result.tripwire,
    )


async def _output_guardrail_fn(
    ctx: RunContextWrapper[Any],
    agent: Agent[Any],
    output: Any,
) -> GuardrailFunctionOutput:
    del ctx, agent
    text = str(output) if output is not None else ""
    result = check_output(text)
    return GuardrailFunctionOutput(
        output_info={"matched": result.matched_pattern},
        tripwire_triggered=result.tripwire,
    )


def build_input_guardrail() -> InputGuardrail[Any]:
    return InputGuardrail(guardrail_function=_input_guardrail_fn, name="d10_input")


def build_output_guardrail() -> OutputGuardrail[Any]:
    return OutputGuardrail(guardrail_function=_output_guardrail_fn, name="d10_output")


__all__ = [
    "D10_FORBIDDEN_PATTERNS",
    "D10_FREEZE_CODE",
    "D10_FREEZE_MESSAGE",
    "GuardrailResult",
    "build_input_guardrail",
    "build_output_guardrail",
    "check_input",
    "check_output",
]
