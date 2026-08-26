"""MCP write tools: research.hypothesis.create / patch / retire (Wave RS-E4.1).

Default dry_run=true → diff preview. dry_run=false requires approval_token.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.mcp.tools._common import with_conn
from bifrost_research.mcp.tools._write_common import (
    WRITE_SUFFIX,
    diff_preview,
    executed_ok,
    require_approval_or_error,
    safe_err,
)
from bifrost_research.repositories import hypothesis as repo


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.hypothesis.create",
        description=(
            "Create a research.hypothesis row. "
            f"{WRITE_SUFFIX}"
        ),
    )
    def create_hypothesis(
        title: str,
        thesis: str,
        symbols: list[str] | None = None,
        tags: list[str] | None = None,
        status: str | None = "active",
        origin_page: str | None = "copilot",
        conclusion: str | None = None,
        dry_run: bool = True,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        args = {
            "title": title,
            "thesis": thesis,
            "symbols": symbols or [],
            "tags": tags or [],
            "status": status or "active",
            "origin_page": origin_page,
            "conclusion": conclusion,
        }
        gate = require_approval_or_error(
            dry_run=dry_run,
            approval_token=approval_token,
            tool="research.hypothesis.create",
            arguments=args,
        )
        if gate is not None:
            return gate

        preview = {
            "title": (title or "").strip(),
            "thesis": (thesis or "").strip(),
            "symbols": list(symbols or []),
            "tags": list(tags or []),
            "status": status or "active",
            "origin_page": origin_page or "copilot",
            "conclusion": conclusion,
        }
        impact = {
            "creates_row": True,
            "table": "research.hypothesis",
            "mutates": ["INSERT"],
        }
        if dry_run:
            return diff_preview(
                diff_kind="create_hypothesis",
                preview=preview,
                impact=impact,
                dry_run=True,
            )

        def _run(conn: Any) -> dict[str, Any]:
            row = repo.create_hypothesis(
                conn,
                title=title,
                thesis=thesis,
                symbols=symbols,
                tags=tags,
                status=status,
                origin_page=origin_page or "copilot",
                origin_ref={"source": "copilot_write"},
                conclusion=conclusion,
            )
            return executed_ok("create_hypothesis", row)

        return with_conn(_run)

    @mcp.tool(
        name="research.hypothesis.patch",
        description=(
            "Patch fields on an existing research.hypothesis. "
            f"{WRITE_SUFFIX}"
        ),
    )
    def patch_hypothesis(
        hypothesis_id: str,
        title: str | None = None,
        thesis: str | None = None,
        symbols: list[str] | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
        conclusion: str | None = None,
        dry_run: bool = True,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title
        if thesis is not None:
            fields["thesis"] = thesis
        if symbols is not None:
            fields["symbols"] = symbols
        if tags is not None:
            fields["tags"] = tags
        if status is not None:
            fields["status"] = status
        if conclusion is not None:
            fields["conclusion"] = conclusion

        args = {"hypothesis_id": hypothesis_id, **fields}
        gate = require_approval_or_error(
            dry_run=dry_run,
            approval_token=approval_token,
            tool="research.hypothesis.patch",
            arguments=args,
        )
        if gate is not None:
            return gate

        preview = {"hypothesis_id": hypothesis_id, "fields": fields}
        impact = {
            "creates_row": False,
            "updates_row": True,
            "table": "research.hypothesis",
            "id": hypothesis_id,
            "mutates": ["UPDATE"],
        }
        if dry_run:
            return diff_preview(
                diff_kind="patch_hypothesis",
                preview=preview,
                impact=impact,
                dry_run=True,
            )

        if not fields:
            return safe_err("no patch fields provided")

        def _run(conn: Any) -> dict[str, Any]:
            row = repo.patch_hypothesis(conn, hypothesis_id, fields)
            if row is None:
                return safe_err(f"hypothesis not found: {hypothesis_id}")
            return executed_ok("patch_hypothesis", row)

        return with_conn(_run)

    @mcp.tool(
        name="research.hypothesis.retire",
        description=(
            "Soft-retire a research.hypothesis (sets retired_at). "
            f"{WRITE_SUFFIX}"
        ),
    )
    def retire_hypothesis(
        hypothesis_id: str,
        dry_run: bool = True,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        args = {"hypothesis_id": hypothesis_id}
        gate = require_approval_or_error(
            dry_run=dry_run,
            approval_token=approval_token,
            tool="research.hypothesis.retire",
            arguments=args,
        )
        if gate is not None:
            return gate

        preview = {"hypothesis_id": hypothesis_id, "action": "retire"}
        impact = {
            "creates_row": False,
            "updates_row": True,
            "table": "research.hypothesis",
            "id": hypothesis_id,
            "mutates": ["UPDATE"],
        }
        if dry_run:
            return diff_preview(
                diff_kind="retire_hypothesis",
                preview=preview,
                impact=impact,
                dry_run=True,
            )

        def _run(conn: Any) -> dict[str, Any]:
            row = repo.retire_hypothesis(conn, hypothesis_id)
            if row is None:
                return safe_err(
                    f"hypothesis not found or already retired: {hypothesis_id}"
                )
            return executed_ok("retire_hypothesis", row)

        return with_conn(_run)
