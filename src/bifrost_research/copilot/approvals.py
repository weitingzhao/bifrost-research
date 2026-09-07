"""HMAC approval tokens for Copilot write tools (Wave RS-E4.2 · D-RS-E-e).

Token is bound to ``(action_id, tool, input_hash)``, TTL 60s, single-use.
Secret: ``COPILOT_APPROVAL_HMAC_SECRET`` (dev fallback for local/tests only).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Any

TTL_SEC = 60
_DEV_FALLBACK_SECRET = "bifrost-copilot-dev-approval-secret-only"

# token_id (nonce) → consumed wall time
_consumed: dict[str, float] = {}
_lock = threading.Lock()


class ApprovalError(Exception):
    """Raised when token validation fails. ``status`` maps to HTTP codes."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def approval_secret_source() -> str:
    """``"env"`` when the signing key is configured, ``"dev_fallback"`` when it is not.

    The fallback is a constant in this repo, and the repo is public: with it in force,
    anyone can mint a valid approval token for any write tool. Nothing surfaced which
    of the two was in use, so a deployment that never set the env looked identical to
    one that did. /health now says which.
    """
    return "env" if os.environ.get("COPILOT_APPROVAL_HMAC_SECRET", "").strip() else "dev_fallback"


def _secret_bytes() -> bytes:
    raw = os.environ.get("COPILOT_APPROVAL_HMAC_SECRET", "").strip()
    if not raw:
        raw = _DEV_FALLBACK_SECRET
    return raw.encode("utf-8")


def reset_consumed_for_tests() -> None:
    with _lock:
        _consumed.clear()


def strip_meta_args(arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Drop dry_run / approval_token from args before hashing or execute."""
    if not arguments:
        return {}
    return {
        k: v
        for k, v in arguments.items()
        if k not in ("dry_run", "approval_token")
    }


def _without_empties(value: Any) -> Any:
    """Drop None / {} / [] recursively — an absent optional and an empty one carry the same intent."""
    if isinstance(value, dict):
        out = {k: _without_empties(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v is not None and v != {} and v != []}
    if isinstance(value, list):
        return [_without_empties(v) for v in value]
    return value


def canonical_input_hash(tool: str, arguments: dict[str, Any] | None) -> str:
    """The hash a token is bound to.

    Computed once when the token is issued over the arguments the Owner saw,
    and again inside the write tool over the arguments it is about to use.
    The tool fills optional arguments with their defaults (``lens_snapshot or
    {}``, ``tags or []``), so a proposal that left them out used to hash
    differently from the same proposal executed — every such approval failed
    as "tampering". Empties are dropped on both sides before hashing.
    """
    payload = {"tool": tool, "args": _without_empties(strip_meta_args(arguments))}
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_token(
    *,
    action_id: str,
    tool: str,
    arguments: dict[str, Any] | None,
    ttl_sec: int = TTL_SEC,
) -> dict[str, Any]:
    """Issue a short-lived HMAC token. Returns token + metadata."""
    if not action_id or not str(action_id).strip():
        raise ApprovalError("action_id required", status=400)
    if not tool or not str(tool).strip():
        raise ApprovalError("tool required", status=400)
    input_hash = canonical_input_hash(tool, arguments)
    exp = int(time.time()) + max(1, int(ttl_sec))
    nonce = secrets.token_hex(8)
    # Pipe-separated so tool names with dots do not break parsing.
    msg = f"{action_id}|{tool}|{input_hash}|{exp}|{nonce}"
    sig = hmac.new(_secret_bytes(), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    token = f"{msg}|{sig}"
    return {
        "approval_token": token,
        "action_id": action_id,
        "tool": tool,
        "input_hash": input_hash,
        "expires_at": exp,
        "expires_in_sec": max(1, int(ttl_sec)),
    }


def _parse_token(token: str) -> tuple[str, str, str, int, str, str]:
    parts = (token or "").split("|")
    if len(parts) != 6:
        raise ApprovalError("malformed approval token", status=400)
    action_id, tool, input_hash, exp_s, nonce, sig = parts
    try:
        exp = int(exp_s)
    except ValueError as exc:
        raise ApprovalError("malformed approval token expiry", status=400) from exc
    return action_id, tool, input_hash, exp, nonce, sig


def validate_token(
    token: str,
    *,
    tool: str,
    arguments: dict[str, Any] | None,
    consume: bool = True,
) -> dict[str, Any]:
    """Validate token against tool + args. Consumes nonce (single-use) by default.

    Raises ``ApprovalError`` with status:
      400 — hash mismatch / malformed
      409 — already consumed (replay)
      410 — expired
    """
    action_id, tok_tool, tok_hash, exp, nonce, sig = _parse_token(token)

    expected_msg = f"{action_id}|{tok_tool}|{tok_hash}|{exp}|{nonce}"
    expected_sig = hmac.new(
        _secret_bytes(), expected_msg.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        raise ApprovalError("invalid approval token signature", status=400)

    if tok_tool != tool:
        raise ApprovalError(
            f"tool mismatch: token bound to {tok_tool!r}, got {tool!r}",
            status=400,
        )

    expected_hash = canonical_input_hash(tool, arguments)
    if not hmac.compare_digest(tok_hash, expected_hash):
        raise ApprovalError("tool input hash mismatch (tampering)", status=400)

    now = int(time.time())
    if now > exp:
        raise ApprovalError("approval token expired", status=410)

    with _lock:
        if nonce in _consumed:
            raise ApprovalError("approval token already consumed", status=409)
        if consume:
            _consumed[nonce] = float(now)
            # prune old entries opportunistically
            cutoff = now - (TTL_SEC * 10)
            stale = [k for k, ts in _consumed.items() if ts < cutoff]
            for k in stale:
                del _consumed[k]

    return {
        "action_id": action_id,
        "tool": tool,
        "input_hash": expected_hash,
        "nonce": nonce,
        "expires_at": exp,
    }


__all__ = [
    "ApprovalError",
    "TTL_SEC",
    "canonical_input_hash",
    "issue_token",
    "reset_consumed_for_tests",
    "strip_meta_args",
    "validate_token",
]


def fill_tool_defaults(mcp: Any, tool: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """The arguments the tool will actually run with, so the token is minted over those.

    A token is signed over what the caller sent; the write tool re-hashes what it is
    about to use, with its declared defaults filled in by FastMCP. Any argument the
    caller left out therefore changed the hash, and every such approval died as
    "tool input hash mismatch (tampering)" — omitting an optional argument is what a
    model does by default, so `research.loop.run_objective` (curate_after=True) could
    not be executed from chat at all. Dropping empty values covered None / {} / [],
    never a non-empty default. Resolving the defaults here fixes every tool at once,
    including tools added later.
    """
    import inspect

    args = dict(strip_meta_args(arguments))
    try:
        registered = mcp._tool_manager.get_tool(tool)  # noqa: SLF001
        fn = getattr(registered, "fn", None)
        if fn is None:
            return args
        params = inspect.signature(fn).parameters
    except Exception:  # noqa: BLE001 — an unknown tool hashes what it was given
        return args
    for name, param in params.items():
        if name in args or name in ("dry_run", "approval_token"):
            continue
        if param.default is inspect.Parameter.empty or param.default is None:
            continue
        args[name] = param.default
    return args

