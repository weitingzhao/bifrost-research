"""MCP tools: research.backtest.* — read-only."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.mcp.tools._common import READ_ONLY_SUFFIX, err, ok, with_conn
from bifrost_research.repositories import backtest_run as repo


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.backtest.list_runs",
        description=(
            "List persisted event-query backtest runs, optionally by hypothesis_id. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def list_runs(
        hypothesis_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = repo.list_runs(
                conn,
                hypothesis_id=hypothesis_id,
                limit=limit,
                offset=offset,
            )
            return ok({"rows": rows, "count": len(rows)})

        return with_conn(_run)

    @mcp.tool(
        name="research.backtest.get_run",
        description=f"Fetch one backtest run by id. {READ_ONLY_SUFFIX}",
    )
    def get_run(run_id: str) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            row = repo.get_run(conn, run_id)
            if row is None:
                return err(f"backtest run not found: {run_id}")
            return ok(row)

        return with_conn(_run)
