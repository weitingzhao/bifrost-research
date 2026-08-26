"""MCP tools: research.vol_surface.* — read-only."""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.mcp.tools._common import READ_ONLY_SUFFIX, err, ok, with_conn
from bifrost_research.repositories import vol_surface as repo


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value.strip()[:10])


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.vol_surface.get_fit",
        description=f"SVI vol-surface fit for symbol (optional trade_date). {READ_ONLY_SUFFIX}",
    )
    def get_fit(symbol: str, trade_date: str | None = None) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = repo.get_fit(conn, symbol, trade_date=_parse_date(trade_date))
            return ok(
                {
                    "rows": rows,
                    "count": len(rows),
                    "symbol": symbol.strip().upper(),
                    "trade_date": trade_date,
                }
            )

        return with_conn(_run)

    @mcp.tool(
        name="research.vol_surface.get_term_structure",
        description=f"IV term structure from surface fits. {READ_ONLY_SUFFIX}",
    )
    def get_term_structure(symbol: str, trade_date: str | None = None) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = repo.get_term_structure(conn, symbol, trade_date=_parse_date(trade_date))
            return ok(
                {
                    "rows": rows,
                    "count": len(rows),
                    "symbol": symbol.strip().upper(),
                }
            )

        return with_conn(_run)

    @mcp.tool(
        name="research.vol_surface.get_residuals",
        description=(
            "Surface residual / misfit rows for one expiry (YYYY-MM-DD). "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def get_residuals(
        symbol: str,
        expiry: str,
        trade_date: str | None = None,
    ) -> dict[str, Any]:
        exp = _parse_date(expiry)
        if exp is None:
            return err("expiry required as YYYY-MM-DD")

        def _run(conn: Any) -> dict[str, Any]:
            rows = repo.get_residuals(
                conn,
                symbol,
                exp,
                trade_date=_parse_date(trade_date),
            )
            return ok({"rows": rows, "count": len(rows), "symbol": symbol.strip().upper()})

        return with_conn(_run)

    @mcp.tool(
        name="research.vol_surface.get_skew_extremes",
        description=f"Cross-symbol skew extremes from surface fits. {READ_ONLY_SUFFIX}",
    )
    def get_skew_extremes(limit: int = 20) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = repo.get_skew_extremes(conn, limit=limit)
            as_of = repo.latest_trade_date(conn)
            return ok({"rows": rows, "count": len(rows), "limit": limit, "as_of": as_of})

        return with_conn(_run)
