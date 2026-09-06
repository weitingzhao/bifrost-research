"""MCP tool: research.exhibit.get — Wave 15, extended by research-loop-automation A2 (read-only)."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.api.exhibit import build_exhibit, exhibit_lens_names
from bifrost_research.mcp.tools._common import READ_ONLY_SUFFIX, err, ok, with_conn


def register(mcp: FastMCP) -> None:
    lens_list = ", ".join(sorted(exhibit_lens_names()))

    @mcp.tool(
        name="research.exhibit.get",
        description=(
            f"Fetch an Analyze Exhibit for a lens ({lens_list}) and symbol. Returns freshness, "
            "readings, history_summary, caveats, plus verdict (band + meaning from the lens "
            "registry), track_record (how this lens' triggers settled at 5d / 20d) and similar "
            "(forward returns after readings like this one). Quote verdict and track_record "
            f"before drilling into per-lens tools. {READ_ONLY_SUFFIX}"
        ),
    )
    def get_exhibit(lens: str, symbol: str) -> dict[str, Any]:
        lens_norm = (lens or "").strip().lower()
        if lens_norm not in exhibit_lens_names():
            return err(f"unknown lens: {lens}")

        def _run(conn: Any) -> dict[str, Any]:
            exhibit = build_exhibit(conn, lens_norm, symbol.strip().upper())
            return ok(exhibit.model_dump())

        return with_conn(_run)
