"""MCP tool: research.exhibit.get — Wave 15 (read-only)."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.api.exhibit import build_exhibit
from bifrost_research.mcp.tools._common import READ_ONLY_SUFFIX, err, ok, with_conn


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.exhibit.get",
        description=(
            "Fetch an Analyze Exhibit for a lens (vrp | iv_rank | terrain | order_sentiment) "
            "and symbol. Returns freshness, readings, history_summary, caveats. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def get_exhibit(lens: str, symbol: str) -> dict[str, Any]:
        lens_norm = (lens or "").strip().lower()
        if lens_norm not in {"vrp", "iv_rank", "terrain", "order_sentiment"}:
            return err(f"unknown lens: {lens}")

        def _run(conn: Any) -> dict[str, Any]:
            exhibit = build_exhibit(conn, lens_norm, symbol.strip().upper())
            return ok(exhibit.model_dump())

        return with_conn(_run)
