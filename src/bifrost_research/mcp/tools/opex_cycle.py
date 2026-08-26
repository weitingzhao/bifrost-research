"""MCP tools: research.opex_cycle.* — read-only."""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.mcp.tools._common import READ_ONLY_SUFFIX, ok, with_conn
from bifrost_research.repositories import opex_cycle as repo


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value.strip()[:10])


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.opex_cycle.get_current",
        description=(
            "Current OpEx / Vanna-Charm daily snapshot for a symbol. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def get_current(symbol: str, trade_date: str | None = None) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            td = _parse_date(trade_date)
            row = repo.get_current(conn, symbol, trade_date=td)
            strike_map = repo.get_vanna_charm_map(conn, symbol, trade_date=td)
            return ok(
                {
                    "row": row,
                    "strike_map": strike_map,
                    "symbol": symbol.strip().upper(),
                }
            )

        return with_conn(_run)

    @mcp.tool(
        name="research.opex_cycle.get_history",
        description=f"OpEx cycle history timeline for a symbol. {READ_ONLY_SUFFIX}",
    )
    def get_history(symbol: str, cycles: int = 12) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = repo.get_history(conn, symbol, cycles=cycles)
            return ok(
                {
                    "rows": rows,
                    "count": len(rows),
                    "symbol": symbol.strip().upper(),
                    "cycles": cycles,
                }
            )

        return with_conn(_run)

    @mcp.tool(
        name="research.opex_cycle.get_pin_analysis",
        description=f"Pin-risk analysis near spot for OpEx. {READ_ONLY_SUFFIX}",
    )
    def get_pin_analysis(symbol: str, cycles: int = 24) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = repo.get_pin_analysis(conn, symbol, cycles=cycles)
            return ok({"rows": rows, "count": len(rows), "symbol": symbol.strip().upper()})

        return with_conn(_run)
