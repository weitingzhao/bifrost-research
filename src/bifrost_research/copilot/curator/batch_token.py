"""CuratorRun batch approval pass — Wave LO-1.

Single HMAC pass bound to ``run_id`` so headless loop_curator can execute
MCP write tools with ``dry_run=false`` without per-tool Owner tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

_PREFIX = "curator-batch|"
_DEV_FALLBACK = "bifrost-curator-batch-dev-only"


def _secret_bytes() -> bytes:
    raw = os.environ.get("COPILOT_APPROVAL_HMAC_SECRET", "").strip()
    if not raw:
        raw = _DEV_FALLBACK
    return raw.encode("utf-8")


def issue_batch_pass(run_id: str, *, ttl_sec: int = 300) -> str:
    if not run_id or not str(run_id).strip():
        raise ValueError("run_id required")
    exp = int(time.time()) + max(30, int(ttl_sec))
    nonce = secrets.token_hex(4)
    msg = f"{run_id.strip()}|{exp}|{nonce}"
    sig = hmac.new(_secret_bytes(), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{_PREFIX}{msg}|{sig}"


def looks_like_batch_pass(token: str | None) -> bool:
    """Whether this was meant to be a batch pass at all."""
    return bool(token) and str(token).strip().startswith(_PREFIX)


def batch_pass_failure(token: str | None, run_id: str | None) -> str | None:
    """Why a batch pass was rejected, or None when it is valid.

    A single "malformed approval token" covered three unrelated situations — no
    curator run in context, a token the model mis-copied, and an expired pass —
    and the headless curator spent an entire run reasoning about a governance
    problem it had no way to identify. Naming the cause is what makes an
    unattended failure diagnosable after the fact.
    """
    if not token:
        return "no approval token supplied"
    if not run_id:
        return "no curator run in context (BIFROST_CURATOR_RUN_ID unset)"
    raw = str(token).strip()
    if not raw.startswith(_PREFIX):
        return "not a batch pass"
    parts = raw[len(_PREFIX) :].split("|")
    if len(parts) != 4:
        return "batch pass is truncated or reshaped"
    tok_run, exp_s, nonce, sig = parts
    if tok_run != str(run_id).strip():
        return f"batch pass belongs to run {tok_run!r}, not {str(run_id).strip()!r}"
    try:
        exp = int(exp_s)
    except ValueError:
        return "batch pass expiry is unreadable"
    if int(time.time()) > exp:
        return f"batch pass expired {int(time.time()) - exp}s ago"
    expected_msg = f"{tok_run}|{exp}|{nonce}"
    expected_sig = hmac.new(
        _secret_bytes(), expected_msg.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return "batch pass signature does not verify — the token was altered in transit"
    return None


def validate_batch_pass(token: str | None, run_id: str | None) -> bool:
    return batch_pass_failure(token, run_id) is None


__all__ = [
    "batch_pass_failure",
    "issue_batch_pass",
    "looks_like_batch_pass",
    "validate_batch_pass",
]
