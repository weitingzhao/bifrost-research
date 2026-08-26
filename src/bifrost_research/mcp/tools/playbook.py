"""MCP read tools: research.playbook.* + research.copilot.* (RS-KB4)."""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.mcp.tools._common import READ_ONLY_SUFFIX, err, ok, with_conn
from bifrost_research.repositories import copilot_session as session_repo
from bifrost_research.repositories import copilot_bridge as bridge_repo
from bifrost_research.repositories import playbook as playbook_repo


def _owner_id() -> str:
    return os.environ.get("RESEARCH_DEFAULT_OWNER", "owner").strip() or "owner"


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.playbook.rules_active",
        description=f"List active playbook rules for the current user. {READ_ONLY_SUFFIX}",
    )
    def rules_active(
        category: str | None = None,
        symbol: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = playbook_repo.list_rules(
                conn,
                owner_id=_owner_id(),
                category=category,
                symbol=symbol,
                tags=tags,
                limit=min(limit, 50),
            )
            return ok({"rows": rows, "count": len(rows)})

        try:
            return with_conn(_run)
        except Exception as exc:  # noqa: BLE001
            return err(str(exc))

    @mcp.tool(
        name="research.playbook.notes_for",
        description=f"List playbook notes (optional symbol filter). {READ_ONLY_SUFFIX}",
    )
    def notes_for(symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = playbook_repo.list_notes(
                conn,
                owner_id=_owner_id(),
                symbol=symbol,
                limit=min(limit, 50),
            )
            return ok({"rows": rows, "count": len(rows)})

        try:
            return with_conn(_run)
        except Exception as exc:  # noqa: BLE001
            return err(str(exc))

    @mcp.tool(
        name="research.playbook.cases_matching",
        description=f"List playbook trade case studies. {READ_ONLY_SUFFIX}",
    )
    def cases_matching(limit: int = 20) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = playbook_repo.list_cases(conn, owner_id=_owner_id(), limit=min(limit, 50))
            return ok({"rows": rows, "count": len(rows)})

        try:
            return with_conn(_run)
        except Exception as exc:  # noqa: BLE001
            return err(str(exc))

    @mcp.tool(
        name="research.playbook.recent_bridge_cases",
        description=f"Recent playbook cases saved from Context Bridge external replies. {READ_ONLY_SUFFIX}",
    )
    def recent_bridge_cases(limit: int = 5) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = bridge_repo.list_recent_bridge_cases(
                conn,
                owner_id=_owner_id(),
                limit=min(limit, 20),
            )
            return ok({"rows": rows, "count": len(rows)})

        try:
            return with_conn(_run)
        except Exception as exc:  # noqa: BLE001
            return err(str(exc))

    @mcp.tool(
        name="research.copilot.recent_sessions",
        description=f"Recent copilot chat sessions (metadata + message counts). {READ_ONLY_SUFFIX}",
    )
    def recent_sessions(limit: int = 10) -> dict[str, Any]:
        def _run(conn: Any) -> dict[str, Any]:
            rows = session_repo.list_recent(conn, owner_id=_owner_id(), limit=min(limit, 30))
            summaries = [
                {
                    "id": r["id"],
                    "title": r.get("title"),
                    "updated_at": r.get("updated_at"),
                    "message_count": len(r.get("messages") or []),
                    "pinned": r.get("pinned"),
                }
                for r in rows
            ]
            return ok({"rows": summaries, "count": len(summaries)})

        try:
            return with_conn(_run)
        except Exception as exc:  # noqa: BLE001
            return err(str(exc))
