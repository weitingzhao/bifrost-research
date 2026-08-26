"""DeepSeek model wiring tests — Wave RS-F2.1."""

from __future__ import annotations

import os

import pytest

from bifrost_research.copilot.models import ModelConfigError, resolve_model_for_agent


def test_deepseek_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ModelConfigError, match="DEEPSEEK_API_KEY"):
        resolve_model_for_agent("deepseek-chat")


def test_deepseek_resolves_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    model = resolve_model_for_agent("deepseek-chat")
    assert model is not None
    assert getattr(model, "model", None) == "deepseek-chat"


def test_deepseek_cost_key_in_providers() -> None:
    from bifrost_research.copilot.providers import estimate_cost

    cost = estimate_cost("deepseek-chat", 1_000_000, 1_000_000)
    assert cost > 0
    assert cost < 1.0
