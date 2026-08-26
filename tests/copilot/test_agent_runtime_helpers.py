"""Unit tests for agent_runtime helpers (Wave RS-UX5 — PENDING fix)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bifrost_research.copilot.agent_runtime import (
    _extract_tool_result,
    _resolve_output_call_id,
)


@dataclass
class _FakeOut:
    output: Any
    raw_item: Any = None


class _RawWithCallId:
    def __init__(self, call_id: str) -> None:
        self.call_id = call_id


class _RawWithId:
    def __init__(self, id_: str) -> None:
        self.id = id_


def test_resolve_output_call_id_prefers_call_id_attr() -> None:
    item = _FakeOut(output={}, raw_item=_RawWithCallId("call_00_abc"))
    assert _resolve_output_call_id(item) == "call_00_abc"


def test_resolve_output_call_id_falls_back_to_id_attr() -> None:
    item = _FakeOut(output={}, raw_item=_RawWithId("call_id_only"))
    assert _resolve_output_call_id(item) == "call_id_only"


def test_resolve_output_call_id_reads_dict_raw() -> None:
    item = _FakeOut(output={}, raw_item={"call_id": "call_dict_1"})
    assert _resolve_output_call_id(item) == "call_dict_1"


def test_resolve_output_call_id_returns_none_when_missing() -> None:
    # No call_id anywhere → runtime FIFO fallback must handle it.
    item = _FakeOut(output={}, raw_item=object())
    assert _resolve_output_call_id(item) is None


def test_extract_tool_result_unwraps_mcp_text_envelope() -> None:
    """MCP tools return `{type: 'text', text: '<json>'}` — must be flattened
    so the FE tool-card can detect `ok=false` for failures."""
    item = _FakeOut(output={"type": "text", "text": '{"ok": true, "data": {"n": 3}}'})
    result = _extract_tool_result(item)
    assert result == {"ok": True, "data": {"n": 3}}


def test_extract_tool_result_passthrough_dict() -> None:
    item = _FakeOut(output={"ok": False, "error": "boom"})
    assert _extract_tool_result(item) == {"ok": False, "error": "boom"}


def test_extract_tool_result_list_of_text_parts() -> None:
    item = _FakeOut(output=[{"type": "text", "text": '{"ok": true, "data": 42}'}])
    assert _extract_tool_result(item) == {"ok": True, "data": 42}


def test_extract_tool_result_json_string_output() -> None:
    item = _FakeOut(output='{"ok": true, "data": "hi"}')
    assert _extract_tool_result(item) == {"ok": True, "data": "hi"}
