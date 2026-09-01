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


def validate_batch_pass(token: str | None, run_id: str | None) -> bool:
    if not token or not run_id:
        return False
    raw = str(token).strip()
    if not raw.startswith(_PREFIX):
        return False
    parts = raw[len(_PREFIX) :].split("|")
    if len(parts) != 4:
        return False
    tok_run, exp_s, nonce, sig = parts
    if tok_run != str(run_id).strip():
        return False
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if int(time.time()) > exp:
        return False
    expected_msg = f"{tok_run}|{exp}|{nonce}"
    expected_sig = hmac.new(
        _secret_bytes(), expected_msg.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_sig, sig)


__all__ = ["issue_batch_pass", "validate_batch_pass"]
