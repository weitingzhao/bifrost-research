"""Smoke helper: list registered Research MCP tools (no SSE required)."""

from __future__ import annotations

from bifrost_research.mcp.server import TOOL_NAMES, list_registered_tool_names


def main() -> None:
    names = list_registered_tool_names()
    print(f"registered={len(names)}")
    for name in names:
        print(name)
    missing = sorted(set(TOOL_NAMES) - set(names))
    if missing:
        raise SystemExit(f"missing tools: {missing}")
    print("ok")


if __name__ == "__main__":
    main()
