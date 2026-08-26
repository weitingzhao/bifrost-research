"""Tests for tool_summarize + bridge_presets (RS-EX2)."""

from __future__ import annotations

from bifrost_research.copilot.bridge_presets import list_presets, validate_focus
from bifrost_research.copilot.tool_summarize import frames_to_context, summarize_tool_result


def test_summarize_tool_result_envelope() -> None:
    text = summarize_tool_result(
        "trade.portfolio.risk_summary",
        {"ok": True, "data": {"symbol": "SPY", "daily_pnl": 12.5}},
    )
    assert "trade.portfolio.risk_summary" in text
    assert "SPY" in text


def test_frames_to_context() -> None:
    ctx = frames_to_context(
        [
            {"kind": "text", "role": "user", "content": "Risk?"},
            {
                "kind": "tool_result",
                "tool_name": "trade.portfolio.risk_summary",
                "ok": True,
                "data": {"symbol": "NVDA"},
            },
            {"kind": "text", "role": "assistant", "content": "NVDA looks ok."},
        ]
    )
    assert "User: Risk?" in ctx
    assert "Assistant: NVDA looks ok." in ctx


def test_list_presets_has_four_focuses() -> None:
    presets = list_presets()
    assert len(presets["focuses"]) == 4
    assert validate_focus("event_driven") == "event_driven"
