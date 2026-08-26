"""Write-tool dry_run enforcement for chat path (Wave RS-F1.2)."""

from __future__ import annotations

from typing import Any

from bifrost_research.mcp.tools._write_common import WRITE_TOOL_NAMES


def is_write_tool(name: str) -> bool:
    return name in WRITE_TOOL_NAMES or any(
        x in name
        for x in (".create", ".patch", ".retire", ".write", ".delete", ".run_event")
    )


def force_chat_dry_run(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Force dry_run=true and strip approval_token for write tools in chat."""
    args = dict(arguments or {})
    if is_write_tool(name):
        args["dry_run"] = True
        args.pop("approval_token", None)
    return args


def coerce_write_dry_run_without_token(*, dry_run: bool, approval_token: str | None) -> bool:
    """Server-side: calls without approval_token always preview (dry_run=true)."""
    if approval_token and str(approval_token).strip():
        return dry_run
    return True


__all__ = ["coerce_write_dry_run_without_token", "force_chat_dry_run", "is_write_tool"]
