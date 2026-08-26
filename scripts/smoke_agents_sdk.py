#!/usr/bin/env python3
"""Smoke: openai-agents connects to Research MCP SSE (:8796)."""

from __future__ import annotations

import asyncio
import os
import sys


async def main() -> int:
    try:
        from agents.mcp import MCPServerSse
    except ImportError:
        print("openai-agents not installed — pip install -e '.[copilot]'")
        return 1

    url = os.environ.get("RESEARCH_MCP_SSE_URL", "http://127.0.0.1:8796/sse")
    server = MCPServerSse(
        params={"url": url},
        cache_tools_list=True,
        name="research-mcp-smoke",
        client_session_timeout_seconds=15.0,
    )
    async with server:
        tools = await server.list_tools()
    print(f"MCP tools discovered: {len(tools)}")
    names = sorted(t.name for t in tools)
    for n in names[:5]:
        print(f"  - {n}")
    if len(names) > 5:
        print(f"  ... +{len(names) - 5} more")
    return 0 if len(tools) >= 28 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
