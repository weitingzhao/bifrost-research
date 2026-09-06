"""Model resolution for the openai-agents SDK (Wave RS-F2.1) and the
chat-completions endpoint table the SDK path and the harness plan step share
(Wave B1 of research-loop-automation).

The agents SDK is the optional ``copilot`` extra, so it is imported lazily —
``resolve_chat_endpoint`` must stay importable from a bare research image.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.models.interface import Model


class ModelConfigError(Exception):
    """Missing API key or invalid model id."""


@dataclass(frozen=True)
class ChatEndpoint:
    """Where a plain chat-completions call for a model id goes."""

    provider: str
    model: str
    base_url: str
    api_key_env: str

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip()


def resolve_chat_endpoint(model_id: str) -> ChatEndpoint:
    """Provider facts for ``model_id``: base URL, key env, resolved model id.

    One table, read by the SDK resolver below and by the harness planner, so
    the two paths cannot drift on which env names a provider. Only providers
    that speak the OpenAI chat-completions protocol belong here.
    """
    lower = (model_id or "").strip().lower()
    if lower.startswith("deepseek"):
        base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        mid = model_id if model_id.startswith("deepseek") else "deepseek-chat"
        return ChatEndpoint("deepseek", mid, base, "DEEPSEEK_API_KEY")
    if lower.startswith(("gpt", "openai")):
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        mid = model_id if model_id.startswith("gpt") else "gpt-4o-mini"
        return ChatEndpoint("openai", mid, base, "OPENAI_API_KEY")
    raise ModelConfigError(f"No chat-completions endpoint for model id {model_id!r}")


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

        endpoint = resolve_chat_endpoint(model_id)
        api_key = _require_key(endpoint.api_key_env, model_id)
        client = AsyncOpenAI(base_url=endpoint.base_url, api_key=api_key)
        return OpenAIChatCompletionsModel(model=endpoint.model, openai_client=client)

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


__all__ = ["ChatEndpoint", "ModelConfigError", "resolve_chat_endpoint", "resolve_model_for_agent"]
