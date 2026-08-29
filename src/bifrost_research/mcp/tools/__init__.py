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
        exhibit,
        hypothesis,
        opex_cycle,
        persona,
        playbook,
        trade_context,
        vol_surface,
        vrp,
        write_backtest,
        write_hypothesis,
        write_loop,
        write_playbook,
    )

    hypothesis.register(mcp)
    backtest.register(mcp)
    vrp.register(mcp)
    exhibit.register(mcp)
    vol_surface.register(mcp)
    opex_cycle.register(mcp)
    discovery.register(mcp)
    trade_context.register(mcp)
    playbook.register(mcp)
    persona.register(mcp)
    write_hypothesis.register(mcp)
    write_backtest.register(mcp)
    write_playbook.register(mcp)
    write_loop.register(mcp)
