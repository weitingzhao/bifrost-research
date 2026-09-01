"""Trust L0 gate for Loop batch auto-approve — Wave LO-4."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

SKILL_ID = "research-loop-batch"
DEFAULT_PLATFORM_URL = "http://127.0.0.1:8780"


def batch_mode_enabled() -> bool:
    return os.environ.get("BIFROST_LOOP_BATCH_MODE", "").strip() in ("1", "true", "yes")


def trust_l0_research_loop_batch() -> bool:
    """True when platform trust matrix lists research-loop-batch at L0."""
    if not batch_mode_enabled():
        return False
    if os.environ.get("BIFROST_LOOP_TRUST_L0_OVERRIDE", "").strip() in ("1", "true", "yes"):
        return True

    base = os.environ.get("PLATFORM_API_URL", DEFAULT_PLATFORM_URL).rstrip("/")
    url = f"{base}/api/v1/agent/governance/trust-matrix"
    try:
        import httpx

        resp = httpx.get(url, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("trust matrix fetch failed: %s", exc)
        return False

    entries = data.get("entries") or []
    for entry in entries:
        if entry.get("skill_id") == SKILL_ID:
            level = str(entry.get("effective_level") or entry.get("level") or "").upper()
            return level == "L0"
    return False


__all__ = ["SKILL_ID", "batch_mode_enabled", "trust_l0_research_loop_batch"]
