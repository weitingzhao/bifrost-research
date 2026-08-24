"""Pluggable LLM providers for AI Forecast (Wave 4.2).

Default is rule-based / heuristic so offline tests pass without API keys.
OpenAI / Anthropic / Ollama stubs activate only when the matching env key is set.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any, Mapping


class LLMProvider(ABC):
    """Minimal chat-completion interface for forecast narrative enrichment."""

    name: str = "base"

    @abstractmethod
    def complete(self, prompt: str, *, system: str | None = None) -> str:
        """Return model text (or heuristic narrative)."""

    def complete_json(self, prompt: str, *, system: str | None = None) -> dict[str, Any]:
        text = self.complete(prompt, system=system)
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        return {"narrative": text, "provider": self.name}


class HeuristicLLMProvider(LLMProvider):
    """Offline default — no network, deterministic narrative from prompt keywords."""

    name = "heuristic"

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        lower = prompt.lower()
        tone = "balanced range day"
        if "crash" in lower or "tail" in lower:
            tone = "defensive — elevated crash-risk regime"
        elif "squeeze" in lower:
            tone = "coiled volatility — watch for squeeze break"
        elif "trending" in lower or "bull" in lower:
            tone = "momentum continuation bias"
        elif "bear" in lower:
            tone = "soft bid — fade strength into resistance"
        return (
            f"[heuristic] Advisory playbook note: {tone}. "
            "D10 BLOCKED — no order placement. "
            f"System={system or 'none'}."
        )


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str | None = None, model: str = "gpt-4o-mini") -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        if not self.api_key:
            return HeuristicLLMProvider().complete(prompt, system=system)
        # Live HTTP is intentionally not invoked in unit tests; stub raises
        # so callers can fall back. Scheduler may wire httpx later.
        raise NotImplementedError(
            "OpenAI live client not bundled; set HeuristicLLMProvider for offline use"
        )


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-20250514") -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        if not self.api_key:
            return HeuristicLLMProvider().complete(prompt, system=system)
        raise NotImplementedError(
            "Anthropic live client not bundled; set HeuristicLLMProvider for offline use"
        )


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str = "llama3.2") -> None:
        self.base_url = (base_url or os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip(
            "/"
        )
        self.model = model

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        # Without an explicit opt-in, stay offline.
        if os.environ.get("RESEARCH_LLM_OLLAMA", "").lower() not in {"1", "true", "yes"}:
            return HeuristicLLMProvider().complete(prompt, system=system)
        raise NotImplementedError(
            "Ollama live client not bundled; set HeuristicLLMProvider for offline use"
        )


def get_default_provider(*, prefer: str | None = None) -> LLMProvider:
    """Pick provider from prefer / env; always safe to call offline."""
    choice = (prefer or os.environ.get("RESEARCH_LLM_PROVIDER") or "heuristic").strip().lower()
    if choice == "openai" and os.environ.get("OPENAI_API_KEY"):
        return OpenAIProvider()
    if choice == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicProvider()
    if choice == "ollama":
        return OllamaProvider()
    return HeuristicLLMProvider()


def enrich_with_llm(
    provider: LLMProvider,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Ask provider for a short narrative; wrap failures with heuristic."""
    prompt = (
        "Summarize an intraday advisory playbook for "
        f"{context.get('symbol')} regime={context.get('regime')} "
        f"scenarios={context.get('scenarios')} "
        f"terrain={context.get('terrain')}."
    )
    try:
        text = provider.complete(
            prompt,
            system="Bifrost Research advisory only. D10 BLOCKED — no orders.",
        )
    except Exception as exc:  # noqa: BLE001 — fall back for any provider error
        text = HeuristicLLMProvider().complete(prompt)
        return {"narrative": text, "provider": "heuristic", "fallback_reason": str(exc)}
    return {"narrative": text, "provider": provider.name}
