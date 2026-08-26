"""Tests for agent persona overlay and preferences (Wave RS-PS)."""

from __future__ import annotations

from bifrost_research.copilot.agents.persona_overlay import render_persona_overlay
from bifrost_research.copilot.agents.persona_preferences import parse_preferences, preferences_for_agent


def test_render_overlay_includes_preferences_and_citation_rule() -> None:
    prefs = {
        "favor_signals": ["breakout"],
        "max_single_position_pct": 15,
    }
    out = render_persona_overlay(
        "portfolio",
        "I like concentrated growth.",
        prefs,
    )
    assert "Owner persona (free text)" in out
    assert "favor_signals" in out
    assert "max_single_position_pct" in out
    assert "cite the slot" in out


def test_validate_overlay_appends_neutral_mandate() -> None:
    out = render_persona_overlay("validate", "Stay skeptical.", {})
    assert "Neutral validation mandate" in out


def test_validate_has_no_preference_slots() -> None:
    prefs = parse_preferences({"favor_signals": ["breakout"]})
    filtered = preferences_for_agent("validate", prefs)
    assert filtered == {}


def test_portfolio_preferences_filtered() -> None:
    prefs = parse_preferences(
        {
            "max_single_position_pct": 12,
            "favor_signals": ["breakout"],
        }
    )
    filtered = preferences_for_agent("portfolio", prefs)
    assert "max_single_position_pct" in filtered
    assert "favor_signals" not in filtered
