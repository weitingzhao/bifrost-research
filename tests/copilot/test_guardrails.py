"""D10 guardrail tests — Wave RS-F1.3."""

from __future__ import annotations

import pytest

from bifrost_research.copilot.guardrails import (
    D10_FORBIDDEN_PATTERNS,
    check_input,
    check_output,
)


@pytest.mark.parametrize(
    "text",
    [
        "Please place_order for NVDA",
        "send ib:operator:cmd now",
        "daemon.start the engine",
        "daemon.scale to 2",
        "set client_id = 42",
        "kubectl apply -f daemon.yaml",
        "run make promote prod",
    ],
)
def test_input_tripwires(text: str) -> None:
    result = check_input(text)
    assert result.tripwire is True
    assert result.reason


def test_safe_research_question_passes() -> None:
    assert check_input("Summarize NVDA VRP over 5 days").tripwire is False
    assert check_input('Explain what "place_order" means in tests').tripwire is False


def test_output_live_trade_recommendation_blocked() -> None:
    result = check_output("Buy 100 shares now with a market order immediately")
    assert result.tripwire is True


def test_pattern_count() -> None:
    assert len(D10_FORBIDDEN_PATTERNS) >= 6
