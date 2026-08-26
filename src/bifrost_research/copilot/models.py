"""Model resolution for openai-agents SDK (Wave RS-F2.1)."""

from __future__ import annotations

import os
from typing import Any

from agents.models.interface import Model


class ModelConfigError(Exception):
    """Missing API key or invalid model id."""


def _require_key(env_name: str, model_label: str) -> str:
    key = os.environ.get(env_name, "").strip()
    if not key:
        raise ModelConfigError(
            f"{env_name} not configured — set secret or choose another model ({model_label})"
        )
    return key


def resolve_model_for_agent(model_id: str) -> Model:
    """Return an SDK Model for the given Copilot model id."""
    lower = (model_id or "").strip().lower()

    if lower.startswith("deepseek"):
        from openai import AsyncOpenAI

        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

        base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        api_key = _require_key("DEEPSEEK_API_KEY", model_id)
        client = AsyncOpenAI(base_url=base, api_key=api_key)
        mid = model_id if model_id.startswith("deepseek") else "deepseek-chat"
        return OpenAIChatCompletionsModel(model=mid, openai_client=client)

    if lower.startswith("ollama") or lower.startswith("llama"):
        from openai import AsyncOpenAI

        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

        base = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        ollama_v1 = base if base.endswith("/v1") else f"{base}/v1"
        client = AsyncOpenAI(base_url=ollama_v1, api_key="ollama")
        mid = model_id.split(":", 1)[-1] if model_id.startswith("ollama:") else model_id
        if mid in ("ollama", ""):
            mid = os.environ.get("OLLAMA_MODEL", "llama3.2")
        return OpenAIChatCompletionsModel(model=mid, openai_client=client)

    if lower.startswith("gpt") or lower.startswith("openai"):
        from openai import AsyncOpenAI

        from agents.models.openai_responses import OpenAIResponsesModel

        api_key = _require_key("OPENAI_API_KEY", model_id)
        client = AsyncOpenAI(api_key=api_key)
        mid = model_id if model_id.startswith("gpt") else "gpt-4o"
        # GPT-5.x (Luna/Terra/Sol/5.5/5.4-nano/5-mini/5) require the Responses
        # API when function tools are combined with reasoning settings; the
        # Chat Completions endpoint rejects both together with HTTP 400 even
        # when reasoning_effort='none'.  For simplicity route *all* OpenAI GPT
        # ids through Responses — it works for 4o / 4.1 too.
        return OpenAIResponsesModel(model=mid, openai_client=client)

    if lower.startswith("claude"):
        try:
            from agents.extensions.models.litellm_model import LitellmModel
        except ImportError as exc:
            raise ModelConfigError(
                "LiteLLM adapter not available for Claude — pip install bifrost-research[copilot]"
            ) from exc
        _require_key("ANTHROPIC_API_KEY", model_id)
        mid = model_id if model_id.startswith("claude") else "claude-sonnet-4-20250514"
        return LitellmModel(model=f"anthropic/{mid}")

    raise ModelConfigError(f"Unknown model id: {model_id!r}")


__all__ = ["ModelConfigError", "resolve_model_for_agent"]
