"""RS-KB1 — turn frame recorder produces full persist payload."""

from __future__ import annotations

from bifrost_research.copilot.agent_runtime import _TurnFrameRecorder


def test_turn_frame_recorder_full_turn_shape() -> None:
    buffer: list[dict] = []
    rec = _TurnFrameRecorder(buffer)

    rec.record_user_text("Premarket brief please")
    rec.record_handoff("triage", "portfolio")
    rec.record_tool_call("call_1", "trade.portfolio.snapshot", {"account": "host"})
    rec.record_tool_result("call_1", "trade.portfolio.snapshot", {"ok": True, "data": {"n": 2}})
    rec.record_token("**Brief** ")
    rec.record_token("NVDA looks extended.")
    rec.finalize()

    kinds = {f["kind"] for f in buffer}
    assert kinds == {"text", "handoff", "tool_call", "tool_result"}
    assert buffer[0]["role"] == "user"
    assert buffer[1]["kind"] == "handoff"
    assert buffer[2]["kind"] == "tool_call"
    assert buffer[3]["kind"] == "tool_result"
    assert buffer[3]["ok"] is True
    assert buffer[-1]["role"] == "assistant"
    assert "NVDA" in buffer[-1]["content"]


def test_turn_frame_recorder_splits_assistant_around_tools() -> None:
    buffer: list[dict] = []
    rec = _TurnFrameRecorder(buffer)
    rec.record_user_text("hi")
    rec.record_token("part one")
    rec.record_tool_call("c1", "research.vrp.summary", {})
    rec.record_tool_result("c1", "research.vrp.summary", {"ok": False, "error": "no data"})
    rec.record_token("part two")
    rec.finalize()

    text_frames = [f for f in buffer if f.get("kind") == "text" and f.get("role") == "assistant"]
    assert len(text_frames) == 2
    assert text_frames[0]["content"] == "part one"
    assert text_frames[1]["content"] == "part two"
    assert buffer[3]["ok"] is False
