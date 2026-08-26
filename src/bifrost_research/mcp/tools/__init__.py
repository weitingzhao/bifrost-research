"""MCP tool modules — register via ``register_all(mcp)`` (read + write)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

__all__ = ["register_all"]


def register_all(mcp: FastMCP) -> None:
    from bifrost_research.mcp.tools import (
        backtest,
        discovery,
        hypothesis,
        opex_cycle,
        trade_context,
        vol_surface,
        vrp,
        write_backtest,
        write_hypothesis,
    )

    hypothesis.register(mcp)
    backtest.register(mcp)
    vrp.register(mcp)
    vol_surface.register(mcp)
    opex_cycle.register(mcp)
    discovery.register(mcp)
    # Wave RS-F5 — Trade System read-only context tools
    trade_context.register(mcp)
    # Wave RS-E4 write tools (dry_run default true)
    write_hypothesis.register(mcp)
    write_backtest.register(mcp)
