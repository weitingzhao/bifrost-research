"""MCP write tools: research.playbook.propose_* → ai_draft inbox (RS-KB4)."""

from __future__ import annotations

import os
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
from bifrost_research.repositories import ai_draft as draft_repo


def _owner_id() -> str:
    return os.environ.get("RESEARCH_DEFAULT_OWNER", "owner").strip() or "owner"


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.playbook.propose_rule",
        description=(
            "Propose a playbook rule draft for inbox approval (or execute with approval_token). "
            f"{WRITE_SUFFIX}"
        ),
    )
    def propose_rule(
        title: str,
        category: str,
        body_md: str,
        trigger_ctx: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        source_session_id: str | None = None,
        dry_run: bool = True,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        args = {
            "title": title,
            "category": category,
            "body_md": body_md,
            "trigger_ctx": trigger_ctx or {},
            "tags": tags or [],
            "source_session_id": source_session_id,
        }
        gate = require_approval_or_error(
            dry_run=dry_run,
            approval_token=approval_token,
            tool="research.playbook.propose_rule",
            arguments=args,
        )
        if gate is not None:
            return gate

        preview = dict(args)
        impact = {
            "creates_row": True,
            "table": "research.ai_draft",
            "kind": "playbook_rule",
            "mutates": ["INSERT"],
        }
        if dry_run:
            return diff_preview(
                diff_kind="playbook_rule",
                preview=preview,
                impact=impact,
                dry_run=True,
            )

        def _run(conn: Any) -> dict[str, Any]:
            row = draft_repo.insert_draft(
                conn,
                kind="playbook_rule",
                payload=preview,
                scope=category or "playbook",
                generated_by="curator_agent",
            )
            return executed_ok("playbook_rule_draft", row)

        return with_conn(_run)

    @mcp.tool(
        name="research.playbook.propose_note",
        description=(
            "Propose a playbook note draft for inbox approval. "
            f"{WRITE_SUFFIX}"
        ),
    )
    def propose_note(
        note_md: str,
        symbols: list[str] | None = None,
        tags: list[str] | None = None,
        source_session_id: str | None = None,
        dry_run: bool = True,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        args = {
            "note_md": note_md,
            "symbols": symbols or [],
            "tags": tags or [],
            "source_session_id": source_session_id,
        }
        gate = require_approval_or_error(
            dry_run=dry_run,
            approval_token=approval_token,
            tool="research.playbook.propose_note",
            arguments=args,
        )
        if gate is not None:
            return gate

        preview = dict(args)
        impact = {
            "creates_row": True,
            "table": "research.ai_draft",
            "kind": "playbook_note",
            "mutates": ["INSERT"],
        }
        if dry_run:
            return diff_preview(
                diff_kind="playbook_note",
                preview=preview,
                impact=impact,
                dry_run=True,
            )

        def _run(conn: Any) -> dict[str, Any]:
            row = draft_repo.insert_draft(
                conn,
                kind="playbook_note",
                payload=preview,
                scope="playbook",
                generated_by="curator_agent",
            )
            return executed_ok("playbook_note_draft", row)

        return with_conn(_run)
