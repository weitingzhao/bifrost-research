"""MCP tools: research.vrp.* — read-only."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.mcp.tools._common import READ_ONLY_SUFFIX, ok, with_conn
from bifrost_research.repositories import vrp as repo


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.vrp.get_latest",
        description=f"Latest VRP (IV-RV spread) row for a symbol. {READ_ONLY_SUFFIX}",
    )
    def get_latest(symbol: str) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            row = repo.get_latest(conn, symbol)
            sym = symbol.strip().upper()
            return ok({"row": row, "symbol": row.get("symbol") if row else sym})

        return with_conn(_run)

    @mcp.tool(
        name="research.vrp.get_history",
        description=f"VRP history for a symbol over N days. {READ_ONLY_SUFFIX}",
    )
    def get_history(symbol: str, days: int = 252) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = repo.get_history(conn, symbol, days=days)
            return ok(
                {
                    "rows": rows,
                    "count": len(rows),
                    "symbol": symbol.strip().upper(),
                    "days": days,
                }
            )

        return with_conn(_run)

    @mcp.tool(
        name="research.vrp.get_extremes",
        description=(
            "Cross-symbol VRP extremes (bucket=high|low). "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def get_extremes(bucket: str = "high", limit: int = 20) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = repo.get_extremes(conn, bucket=bucket, limit=limit)
            as_of = repo.latest_trade_date(conn)
            return ok(
                {
                    "rows": rows,
                    "count": len(rows),
                    "bucket": bucket,
                    "limit": limit,
                    "as_of": as_of,
                }
            )

        return with_conn(_run)
