"""Agent graph construction tests — Wave RS-F3.1."""

from __future__ import annotations

import pytest

from bifrost_research.copilot.agents.graph import (
    build_analyze_agent,
    build_discovery_agent,
    build_explain_agent,
    build_triage_agent,
    build_validate_agent,
    build_verdict_agent,
    build_write_agent,
)
from bifrost_research.copilot.models import ModelConfigError


@pytest.fixture(autouse=True)
def _deepseek_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")


def test_specialists_construct() -> None:
    model = "deepseek-chat"
    assert build_discovery_agent(model).name == "discovery"
    assert build_analyze_agent(model).name == "analyze"
    assert build_validate_agent(model).name == "validate"
    assert build_write_agent(model).name == "write"
    assert build_explain_agent(model).name == "explain"
    assert build_verdict_agent(model).name == "verdict"


def test_triage_has_handoffs() -> None:
    triage = build_triage_agent("deepseek-chat")
    assert triage.name == "triage"
    assert triage.handoffs
    assert len(triage.handoffs) >= 6


def test_verdict_has_as_tool_specialists() -> None:
    verdict = build_verdict_agent("deepseek-chat")
    assert verdict.tools
    assert len(verdict.tools) >= 3
