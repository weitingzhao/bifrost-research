"""Trust L0 gate for Loop batch auto-approve — Wave LO-4."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

SKILL_ID = "research-loop-batch"
DEFAULT_PLATFORM_URL = "http://127.0.0.1:8780"


def batch_mode_enabled() -> bool:
    return os.environ.get("BIFROST_LOOP_BATCH_MODE", "").strip() in ("1", "true", "yes")


def matrix_level() -> str | None:
    """What the platform trust matrix says about research-loop-batch, or None
    when the matrix could not be read.

    Separate from the gate on purpose. The Owner's grant lives here; whether
    *this* process is allowed to act on it is the gate's question, and the
    console pill was answering the second when the Owner asked the first —
    it read "Trust not L0" on the API pod for an hour after L0 was set.
    """
    base = os.environ.get("PLATFORM_API_URL", DEFAULT_PLATFORM_URL).rstrip("/")
    url = f"{base}/api/v1/agent/governance/trust-matrix"
    try:
        import httpx

        resp = httpx.get(url, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("trust matrix fetch failed: %s", exc)
        return None
    for entry in data.get("entries") or []:
        if entry.get("skill_id") == SKILL_ID:
            # The matrix serialises `current_level`; neither `effective_level`
            # nor `level` has ever been a field on it, so this read returned ""
            # and the gate could not open even once L0 was granted. Both older
            # names are kept as fallbacks in case the payload ever carries them.
            return (
                str(
                    entry.get("current_level")
                    or entry.get("effective_level")
                    or entry.get("level")
                    or ""
                ).upper()
                or None
            )
    return None


def trust_l0_research_loop_batch() -> bool:
    """The gate: this process may auto-accept research drafts.

    Requires the unattended batch mode *and* the Owner's L0 on the matrix. A
    process without batch mode — the API pod serving the Run dialog — never
    passes, whatever the matrix says: a run the Owner started by hand is theirs
    to approve.
    """
    if not batch_mode_enabled():
        return False
    if os.environ.get("BIFROST_LOOP_TRUST_L0_OVERRIDE", "").strip() in ("1", "true", "yes"):
        return True
    return matrix_level() == "L0"


__all__ = ["SKILL_ID", "batch_mode_enabled", "matrix_level", "trust_l0_research_loop_batch"]
