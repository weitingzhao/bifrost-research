"""Per-owner bridge rate limit — 6 requests / minute (D3)."""

from __future__ import annotations

import threading
import time

BRIDGE_LIMIT_PER_MINUTE = 6

_lock = threading.Lock()
_hits: dict[str, list[float]] = {}


def check_bridge_rate_limit(owner_id: str) -> tuple[bool, int]:
    """Return (allowed, retry_after_sec). retry_after_sec is 0 when allowed."""
    now = time.time()
    window_start = now - 60.0
    with _lock:
        bucket = [t for t in _hits.get(owner_id, []) if t >= window_start]
        if len(bucket) >= BRIDGE_LIMIT_PER_MINUTE:
            oldest = min(bucket)
            retry = max(1, int(60 - (now - oldest)) + 1)
            _hits[owner_id] = bucket
            return False, retry
        bucket.append(now)
        _hits[owner_id] = bucket
        return True, 0


def reset_bridge_rate_limit_for_tests() -> None:
    with _lock:
        _hits.clear()


__all__ = ["BRIDGE_LIMIT_PER_MINUTE", "check_bridge_rate_limit", "reset_bridge_rate_limit_for_tests"]
