"""Wave RS-F1.1 — openai-agents SDK bootstrap tests."""

from __future__ import annotations

import pytest

from bifrost_research.mcp.server import ALL_TOOL_NAMES, create_mcp_server


def test_openai_agents_import() -> None:
    import agents  # noqa: F401

    assert agents is not None


def test_mcp_tool_count_at_least_28() -> None:
    mcp = create_mcp_server()
    tools = mcp._tool_manager.list_tools()  # noqa: SLF001
    names = {t.name for t in tools}
    assert len(names) >= 28
    assert set(ALL_TOOL_NAMES).issubset(names)


@pytest.mark.parametrize(
    "module_path",
    [
        "bifrost_research.copilot.agent_runtime",
        "bifrost_research.copilot.agents.graph",
        "bifrost_research.copilot.guardrails",
        "bifrost_research.copilot.models",
    ],
)
def test_copilot_modules_import(module_path: str) -> None:
    import importlib

    mod = importlib.import_module(module_path)
    assert mod is not None
