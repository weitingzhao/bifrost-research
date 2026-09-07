"""The chat's tool results are the tool's own envelope, whatever the SDK's return shape.

mcp ≥ 1.10 made FastMCP.call_tool return ``(content, structured)``; the legacy
reader parsed neither half and answered ``{"ok": True, "data": []}`` — so every
chat-approved write (Approve on a DiffApprovalCard, POST /research/copilot/execute)
reported success, wrote nothing, and logged ``executed``. Found on the D4
acceptance: the propose_candidate execute returned ``data: []`` and no row.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from bifrost_research.copilot.orchestrator_legacy import _dispatch_tool, tool_result_envelope
from bifrost_research.copilot.orchestrator_legacy import ToolCallRequest

ENVELOPE = {"ok": True, "data": {"diff_kind": "candidate_batch", "executed": True, "result": {"id": "cand_1"}}}


def _text(payload: Any) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=json.dumps(payload))


def test_every_sdk_shape_yields_the_tools_envelope() -> None:
    assert tool_result_envelope(ENVELOPE) == ENVELOPE  # plain dict
    assert tool_result_envelope([_text(ENVELOPE)]) == ENVELOPE  # content blocks only
    assert tool_result_envelope(([_text(ENVELOPE)], ENVELOPE)) == ENVELOPE  # (content, structured)
    assert tool_result_envelope(([], ENVELOPE)) == ENVELOPE  # structured only
    assert tool_result_envelope(([_text([1, 2])], {"result": [1, 2]})) == {"ok": True, "data": [1, 2]}  # non-object return
    assert tool_result_envelope(([], {"result": ENVELOPE})) == ENVELOPE  # wrapped object return
    obj = SimpleNamespace(content=[_text(ENVELOPE)], structured_content=None)
    assert tool_result_envelope(obj) == ENVELOPE  # a ToolResult-like object
    assert tool_result_envelope(([], {})) == {"ok": True, "data": []}  # genuinely empty stays empty


def test_dispatch_returns_the_error_envelope_a_write_tool_produced() -> None:
    refused = {"ok": False, "error": "400: tool input hash mismatch (tampering)", "status": 400}

    class _Mcp:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            assert arguments["dry_run"] is False and arguments["approval_token"] == "tok"
            return ([_text(refused)], refused)

    out = asyncio.run(
        _dispatch_tool(
            _Mcp(),
            ToolCallRequest(id="exec", name="research.loop.propose_candidate", arguments={"symbol": "NVDA", "dry_run": False, "approval_token": "tok"}),
            force_write_dry_run=False,
        )
    )
    assert out == refused  # not {"ok": True, "data": []}
