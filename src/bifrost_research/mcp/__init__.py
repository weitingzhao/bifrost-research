"""Research MCP server — read-only tools over Golden Source (Wave RS-E2).

Standalone process on ``:8796`` (D-RS-E-d / D-RS-E-h). Zero write tools.
"""

from __future__ import annotations

__all__ = ["create_mcp_server", "TOOL_NAMES"]


def __getattr__(name: str):
    if name == "create_mcp_server":
        from bifrost_research.mcp.server import create_mcp_server

        return create_mcp_server
    if name == "TOOL_NAMES":
        from bifrost_research.mcp.server import TOOL_NAMES

        return TOOL_NAMES
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
