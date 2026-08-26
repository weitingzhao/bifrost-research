"""MCP tools: research.hypothesis.* — read-only."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.mcp.tools._common import READ_ONLY_SUFFIX, err, ok, with_conn
from bifrost_research.repositories import hypothesis as repo


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.hypothesis.list",
        description=(
            "List research hypotheses with optional status/symbol/tag filters. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def list_hypotheses(
        status: str | None = None,
        symbol: str | None = None,
        tag: str | None = None,
        include_retired: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = repo.list_hypotheses(
                conn,
                status=status,
                symbol=symbol,
                tag=tag,
                include_retired=include_retired,
                limit=limit,
                offset=offset,
            )
            return ok({"rows": rows, "count": len(rows)})

        return with_conn(_run)

    @mcp.tool(
        name="research.hypothesis.list_active",
        description=(
            "List active (non-retired) hypotheses, optionally filtered by symbol. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def list_active(
        symbol: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = repo.list_hypotheses(
                conn,
                status="active",
                symbol=symbol,
                include_retired=False,
                limit=limit,
                offset=offset,
            )
            return ok({"rows": rows, "count": len(rows), "status": "active"})

        return with_conn(_run)

    @mcp.tool(
        name="research.hypothesis.get",
        description=f"Fetch one hypothesis by id. {READ_ONLY_SUFFIX}",
    )
    def get_hypothesis(hypothesis_id: str) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            row = repo.get_hypothesis(conn, hypothesis_id)
            if row is None:
                return err(f"hypothesis not found: {hypothesis_id}")
            return ok(row)

        return with_conn(_run)

    @mcp.tool(
        name="research.hypothesis.summary_active",
        description=(
            "Active-hypothesis summary (counts + top-N recent). "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def summary_active(top_n: int = 5) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            return ok(repo.active_summary(conn, top_n=top_n))

        return with_conn(_run)
