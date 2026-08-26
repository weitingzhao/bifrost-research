"""Multi-provider LLM abstraction (D-RS-E-c).

Soft-imports ``anthropic`` / ``openai``. Missing keys or packages yield a clear
error string — tests inject a mock provider and never need live API keys.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ProviderTurn:
    """One provider response turn (text + optional tool calls)."""

    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    # Opaque provider-native messages to append for multi-turn tool loops
    assistant_payload: Any = None


class LlmProvider(Protocol):
    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        model: str,
    ) -> ProviderTurn: ...


# Rough public pricing for cap accounting (USD per 1M tokens) — estimates only.
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude": (3.0, 15.0),
    "gpt": (2.5, 10.0),
    "deepseek": (0.14, 0.28),
    "ollama": (0.0, 0.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    key = "claude"
    lower = model.lower()
    if lower.startswith("deepseek"):
        key = "deepseek"
    elif lower.startswith("gpt") or "openai" in lower:
        key = "gpt"
    elif lower.startswith("ollama") or lower.startswith("llama"):
        key = "ollama"
    pin, pout = _PRICE_PER_MTOK[key]
    return (input_tokens / 1_000_000.0) * pin + (output_tokens / 1_000_000.0) * pout


def resolve_provider(model: str) -> LlmProvider:
    lower = (model or "").lower()
    if lower.startswith("ollama") or lower.startswith("llama"):
        return OllamaProvider()
    if lower.startswith("gpt") or lower.startswith("openai"):
        return OpenAIProvider()
    return ClaudeProvider()


class ClaudeProvider:
    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        model: str,
    ) -> ProviderTurn:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return ProviderTurn(
                error="ANTHROPIC_API_KEY not configured — set secret or choose another model"
            )
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError:
            return ProviderTurn(
                error="anthropic package not installed — pip install bifrost-research[copilot]"
            )

        client = anthropic.Anthropic(api_key=api_key)
        tool_defs = [
            {
                "name": t.name.replace(".", "_"),  # Anthropic name charset
                "description": t.description,
                "input_schema": t.input_schema or {"type": "object", "properties": {}},
            }
            for t in tools
        ]
        # Map sanitized names back
        name_map = {t.name.replace(".", "_"): t.name for t in tools}

        system = ""
        api_messages: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                system = str(content)
                continue
            if role == "tool":
                api_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.get("tool_call_id", ""),
                                "content": str(content),
                            }
                        ],
                    }
                )
                continue
            if role == "assistant" and msg.get("assistant_payload") is not None:
                api_messages.append(msg["assistant_payload"])
                continue
            api_messages.append({"role": role, "content": content})

        model_id = model if model.startswith("claude") else "claude-sonnet-4-20250514"
        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": 4096,
            "messages": api_messages or [{"role": "user", "content": "Hello"}],
        }
        if system:
            kwargs["system"] = system
        if tool_defs:
            kwargs["tools"] = tool_defs

        try:
            resp = client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            return ProviderTurn(error=f"Claude API error: {exc}")

        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif btype == "tool_use":
                raw_name = getattr(block, "name", "") or ""
                real_name = name_map.get(raw_name, raw_name.replace("_", ".", 2))
                # Prefer exact map; fallback reconstruct research.domain.action
                if raw_name in name_map:
                    real_name = name_map[raw_name]
                else:
                    # reverse: research_hypothesis_list_active → research.hypothesis.list_active
                    real_name = _unsanitize_tool_name(raw_name, {t.name for t in tools})
                inp = getattr(block, "input", None) or {}
                tool_calls.append(
                    ToolCallRequest(
                        id=getattr(block, "id", "") or f"tool_{len(tool_calls)}",
                        name=real_name,
                        arguments=dict(inp) if isinstance(inp, dict) else {},
                    )
                )

        usage = getattr(resp, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        return ProviderTurn(
            text="".join(text_parts),
            tool_calls=tool_calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=estimate_cost(model, in_tok, out_tok),
            assistant_payload={"role": "assistant", "content": resp.content},
        )


class OpenAIProvider:
    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        model: str,
    ) -> ProviderTurn:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return ProviderTurn(
                error="OPENAI_API_KEY not configured — set secret or choose another model"
            )
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
        except ImportError:
            return ProviderTurn(
                error="openai package not installed — pip install bifrost-research[copilot]"
            )

        client = OpenAI(api_key=api_key)
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": t.name.replace(".", "_"),
                    "description": t.description,
                    "parameters": t.input_schema
                    or {"type": "object", "properties": {}},
                },
            }
            for t in tools
        ]
        name_map = {t.name.replace(".", "_"): t.name for t in tools}

        api_messages: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            if role == "tool":
                api_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.get("tool_call_id", ""),
                        "content": str(msg.get("content", "")),
                    }
                )
            elif role == "assistant" and msg.get("assistant_payload") is not None:
                api_messages.append(msg["assistant_payload"])
            else:
                api_messages.append({"role": role, "content": msg.get("content", "")})

        model_id = model if model.startswith("gpt") else "gpt-4o"
        try:
            resp = client.chat.completions.create(
                model=model_id,
                messages=api_messages,
                tools=tool_defs or None,
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderTurn(error=f"OpenAI API error: {exc}")

        choice = resp.choices[0].message
        text = choice.content or ""
        tool_calls: list[ToolCallRequest] = []
        for tc in choice.tool_calls or []:
            raw_name = tc.function.name
            real_name = name_map.get(raw_name) or _unsanitize_tool_name(
                raw_name, {t.name for t in tools}
            )
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCallRequest(id=tc.id, name=real_name, arguments=args)
            )

        usage = resp.usage
        in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
        out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
        return ProviderTurn(
            text=text,
            tool_calls=tool_calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=estimate_cost(model, in_tok, out_tok),
            assistant_payload={
                "role": "assistant",
                "content": text,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in (choice.tool_calls or [])
                ]
                or None,
            },
        )


class OllamaProvider:
    """Optional local Ollama via OpenAI-compatible HTTP. No key required."""

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        model: str,
    ) -> ProviderTurn:
        base = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        # Strip ollama: prefix
        model_id = model.split(":", 1)[-1] if model.startswith("ollama:") else model
        if model_id in ("ollama", ""):
            model_id = os.environ.get("OLLAMA_MODEL", "llama3.2")

        try:
            import httpx
        except ImportError:
            return ProviderTurn(error="httpx required for Ollama provider")

        # Tools unsupported in basic Ollama chat — answer from context only.
        api_messages = [
            {"role": m.get("role", "user"), "content": str(m.get("content", ""))}
            for m in messages
            if m.get("role") in ("system", "user", "assistant")
        ]
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    f"{base}/api/chat",
                    json={"model": model_id, "messages": api_messages, "stream": False},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return ProviderTurn(error=f"Ollama error: {exc}")

        text = ""
        msg = data.get("message") or {}
        if isinstance(msg, dict):
            text = str(msg.get("content") or "")
        return ProviderTurn(text=text, cost_usd=0.0)


def _unsanitize_tool_name(raw: str, known: set[str]) -> str:
    if raw in known:
        return raw
    # research_hypothesis_list_active → try progressive dots
    if raw.startswith("research_"):
        candidate = "research." + raw[len("research_") :].replace("_", ".", 1)
        # Better: match against known by normalizing
        norm = raw.replace(".", "_")
        for name in known:
            if name.replace(".", "_") == norm:
                return name
        # Fallback three-part reconstruction for research.domain.action(_more)
        parts = raw.split("_")
        if len(parts) >= 3 and parts[0] == "research":
            domain = parts[1]
            action = "_".join(parts[2:])
            guess = f"research.{domain}.{action}"
            if guess in known:
                return guess
    return raw
