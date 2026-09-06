"""research.lenses.list — registered, read-only, and returns the registry."""

from __future__ import annotations

from bifrost_research.mcp.server import TOOL_NAMES, create_mcp_server


def test_lenses_tool_registered_and_answers() -> None:
    assert "research.lenses.list" in TOOL_NAMES
    mcp = create_mcp_server()
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}  # noqa: SLF001
    tool = tools["research.lenses.list"]
    out = tool.fn()
    assert out["ok"] is True
    assert out["data"]["count"] >= 10
    assert any(r["id"] == "vrp" for r in out["data"]["lenses"])
