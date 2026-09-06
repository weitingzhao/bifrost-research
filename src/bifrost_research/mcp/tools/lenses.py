"""MCP tool: research.lenses.list — the lens registry, read-only, no database."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.lenses.registry import REGISTRY_VERSION, public_registry, score_bands
from bifrost_research.mcp.tools._common import READ_ONLY_SUFFIX, ok


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.lenses.list",
        description=(
            "Lens registry: every Analyze lens with its bands (hot / lean_hot / neutral / "
            "lean_cold / cold), what hot and cold mean for a premium seller, source table, "
            "page route and data dependency. Use it before reading an exhibit or a hit-rate "
            f"so verdict words match the engines' thresholds. {READ_ONLY_SUFFIX}"
        ),
    )
    def list_lenses() -> dict[str, Any]:
        lenses = public_registry()
        return ok(
            {
                "version": REGISTRY_VERSION,
                "score_bands": score_bands(),
                "lenses": lenses,
                "count": len(lenses),
            }
        )
