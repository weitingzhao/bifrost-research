"""Report a finished Loop batch to the platform trust matrix.

`trust_gate` reads the matrix to decide whether the batch may auto-approve.
Nothing wrote to it. Across 694 recorded jobs the `research.loop.batch` scope had
zero entries — every scope the matrix could count came from an agent session the
remediation runner itself started, and the Loop runs on its own CronJob. So the
counter sat at 0 while the batch ran green for weeks, and no amount of success
could move it: the gate was not strict, it was disconnected.

Reporting only earns *eligibility*. Promotion to L0 is still an operator-gated
override, which is the right shape — autonomy is proposed by evidence and
granted by the Owner.

Best-effort throughout: a run that did its work must not be marked failed because
the control plane was unreachable.
"""

from __future__ import annotations

import logging
import os

from bifrost_research.copilot.harness.trust_gate import DEFAULT_PLATFORM_URL, SKILL_ID

logger = logging.getLogger(__name__)


def report_batch_outcome(*, ok: bool, summary: str = "") -> bool:
    """Record one Loop batch run. Returns True when the platform accepted it.

    Deliberately reads only PLATFORM_REPORTER_TOKEN and never falls back to an
    operator token. Operator also carries `POST /cluster/workloads/scale` —
    which D10 forbids pointing at the trade daemon — and
    `PUT /agent/governance/trust-overrides/{skill_id}`, which would let this
    Loop grant itself the L0 the trust gate exists to withhold. A reporter token
    can record an outcome and do nothing else, so that is the only credential
    this path will use.
    """
    token = os.environ.get("PLATFORM_REPORTER_TOKEN", "").strip()
    if not token:
        logger.info(
            "trust report skipped: no PLATFORM_REPORTER_TOKEN — %s stays at 0 "
            "consecutive successes",
            SKILL_ID,
        )
        return False

    base = os.environ.get("PLATFORM_API_URL", DEFAULT_PLATFORM_URL).rstrip("/")
    url = f"{base}/api/v1/agent/governance/skill-runs"
    try:
        import httpx

        resp = httpx.post(
            url,
            json={
                "scope": SKILL_ID,
                "status": "done" if ok else "failed",
                "summary": summary[:2000],
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=8.0,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("trust report failed for %s: %s", SKILL_ID, exc)
        return False
    logger.info("trust report recorded for %s: %s", SKILL_ID, "done" if ok else "failed")
    return True


__all__ = ["report_batch_outcome"]
