"""Shared helpers for write-mode MCP tools (Wave RS-E4.1)."""

from __future__ import annotations

import os
from typing import Any

from bifrost_research.copilot.approvals import ApprovalError, validate_token
from bifrost_research.copilot.curator.batch_token import (
    batch_pass_failure,
    looks_like_batch_pass,
)
from bifrost_research.mcp.tools._common import err

WRITE_SUFFIX = (
    "**Write tool**. Default dry_run=true returns a diff preview. "
    "dry_run=false requires a valid approval_token (D-RS-E-e)."
)

WRITE_TOOL_NAMES: tuple[str, ...] = (
    "research.hypothesis.create",
    "research.hypothesis.patch",
    "research.hypothesis.retire",
    "research.backtest.run_event_query",
    "research.playbook.propose_rule",
    "research.playbook.propose_note",
    # Wave C — Research Loop
    "research.loop.propose_candidate",
    "research.loop.promote_to_hypothesis",
    "research.loop.attach_backtest_evidence",
    "research.loop.draft_decision",
    "research.loop.propose_order_intent",
)


def require_approval_or_error(
    *,
    dry_run: bool,
    approval_token: str | None,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any] | None:
    """Return an error envelope if execute is not allowed; else None (proceed)."""
    if dry_run:
        return None
    curator_run_id = os.environ.get("BIFROST_CURATOR_RUN_ID", "").strip()
    if approval_token and looks_like_batch_pass(approval_token):
        # A batch pass gets its own verdict. Falling through to the per-tool
        # validator turned "no curator run in context", "the model mis-copied
        # the token" and "the pass expired" into one indistinguishable
        # "malformed approval token" — an unattended failure nobody could
        # diagnose after the fact.
        reason = batch_pass_failure(approval_token, curator_run_id or None)
        if reason is None:
            return None
        return {
            "ok": False,
            "error": f"403: batch pass rejected — {reason}",
            "status": 403,
        }
    if not approval_token or not str(approval_token).strip():
        return {
            "ok": False,
            "error": "403: approval token required",
            "status": 403,
        }
    if curator_run_id:
        # A curator run was in context but what arrived was not the batch pass
        # it was handed. The pass is a ~110-character HMAC containing pipes that
        # the model has to copy out of its prompt into a tool argument, so it
        # goes missing far more readily than a design that never asked it to.
        # Falling through to the per-tool validator here reports a token problem
        # and hides the transcription one.
        return {
            "ok": False,
            "error": (
                "403: batch pass rejected — a curator run is in context but the "
                f"token supplied is not its batch pass (got {len(str(approval_token).strip())} "
                "chars with no curator-batch prefix)"
            ),
            "status": 403,
        }
    try:
        validate_token(
            str(approval_token).strip(),
            tool=tool,
            arguments=arguments,
            consume=True,
        )
    except ApprovalError as exc:
        return {
            "ok": False,
            "error": f"{exc.status}: {exc.message}",
            "status": exc.status,
        }
    return None


def diff_preview(
    *,
    diff_kind: str,
    preview: dict[str, Any],
    impact: dict[str, Any],
    dry_run: bool = True,
) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "diff_kind": diff_kind,
            "preview": preview,
            "impact": impact,
            "dry_run": dry_run,
        },
    }


def executed_ok(diff_kind: str, result: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "diff_kind": diff_kind,
            "result": result,
            "dry_run": False,
            "executed": True,
        },
    }


def safe_err(message: str) -> dict[str, Any]:
    return err(message)


__all__ = [
    "WRITE_SUFFIX",
    "WRITE_TOOL_NAMES",
    "diff_preview",
    "executed_ok",
    "require_approval_or_error",
    "safe_err",
]
