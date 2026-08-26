"""Daily AI cost / token cap (D-RS-E-i).

Default ``COPILOT_DAILY_CAP_USD=2.0`` for dev. In-memory process-local counter —
resets at UTC midnight. Sufficient for single-replica research-api; multi-replica
can later share Redis without changing the HTTP contract.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class UsageSnapshot:
    tokens_today: int
    cost_estimate_usd: float
    cap_usd: float
    remaining_usd: float
    day_utc: str


_lock = threading.Lock()
_day_utc: str = datetime.now(timezone.utc).date().isoformat()
_tokens_today: int = 0
_cost_today: float = 0.0


def _cap_usd() -> float:
    raw = os.environ.get("COPILOT_DAILY_CAP_USD", "2.0")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 2.0


def _roll_day_locked() -> None:
    global _day_utc, _tokens_today, _cost_today
    today = datetime.now(timezone.utc).date().isoformat()
    if today != _day_utc:
        _day_utc = today
        _tokens_today = 0
        _cost_today = 0.0


def get_usage() -> UsageSnapshot:
    with _lock:
        _roll_day_locked()
        cap = _cap_usd()
        remaining = max(0.0, cap - _cost_today)
        return UsageSnapshot(
            tokens_today=_tokens_today,
            cost_estimate_usd=round(_cost_today, 6),
            cap_usd=cap,
            remaining_usd=round(remaining, 6),
            day_utc=_day_utc,
        )


def check_rate_limit() -> UsageSnapshot | None:
    """Return usage snapshot if under cap; ``None`` means blocked (caller → 429)."""
    snap = get_usage()
    if snap.remaining_usd <= 0:
        return None
    return snap


def record_usage(*, tokens: int, cost_usd: float) -> UsageSnapshot:
    global _tokens_today, _cost_today
    with _lock:
        _roll_day_locked()
        _tokens_today += max(0, int(tokens))
        _cost_today += max(0.0, float(cost_usd))
        cap = _cap_usd()
        return UsageSnapshot(
            tokens_today=_tokens_today,
            cost_estimate_usd=round(_cost_today, 6),
            cap_usd=cap,
            remaining_usd=round(max(0.0, cap - _cost_today), 6),
            day_utc=_day_utc,
        )


def reset_usage_for_tests() -> None:
    """Test helper — clear counters."""
    global _day_utc, _tokens_today, _cost_today
    with _lock:
        _day_utc = datetime.now(timezone.utc).date().isoformat()
        _tokens_today = 0
        _cost_today = 0.0


def usage_to_dict(snap: UsageSnapshot) -> dict[str, Any]:
    return {
        "tokens_today": snap.tokens_today,
        "cost_estimate_usd": snap.cost_estimate_usd,
        "cap_usd": snap.cap_usd,
        "remaining_usd": snap.remaining_usd,
        "day_utc": snap.day_utc,
    }
