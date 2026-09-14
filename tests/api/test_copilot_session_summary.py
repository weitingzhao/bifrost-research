"""Session list summary — turns (D5) and cost field shape."""

from __future__ import annotations

from bifrost_research.api.copilot_sessions import _count_turns, _summary


def test_count_turns_user_text_only() -> None:
    frames = [
        {"kind": "text", "role": "user", "content": "hi"},
        {"kind": "text", "role": "assistant", "content": "yo"},
        {"kind": "tool_call", "role": "assistant"},
        {"kind": "text", "role": "user", "content": "again"},
        {"kind": "handoff", "agent_from": "triage", "agent_to": "vol"},
    ]
    assert _count_turns(frames) == 2


def test_summary_includes_turns_and_cost() -> None:
    row = {
        "id": "s1",
        "title": "T",
        "model": "deepseek-chat",
        "updated_at": "2026-09-14T12:00:00Z",
        "messages": [
            {"kind": "text", "role": "user", "content": "a"},
            {"kind": "text", "role": "assistant", "content": "b"},
        ],
        "pinned": False,
        "group_name": None,
        "candidate_ids": [],
        "origin_page": "/research",
        "origin_label": "Research",
        "origin_symbol": "AAPL",
    }
    out = _summary(row, writes={"proposed": 1}, cost_usd=0.0042)
    assert out["turns"] == 1
    assert out["message_count"] == 2
    assert out["cost_usd"] == 0.0042
    assert out["writes"] == {"proposed": 1}
