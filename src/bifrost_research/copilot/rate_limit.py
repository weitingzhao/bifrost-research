"""Daily AI cost / token cap (D-RS-E-i) — plus per-provider caps for the
persona judges (research-loop-automation B2, D-RLA-3).

Default ``COPILOT_DAILY_CAP_USD=2.0`` for dev. In-memory process-local counter —
resets at UTC midnight. Sufficient for single-replica research-api; multi-replica
can later share Redis without changing the HTTP contract.

The provider counters are also process-local, but the harness Cron is a fresh
process every day, so a cap that lived only in memory would reset on every run.
``seed_provider_cost`` lets the caller replay today's spend from
``research.ai_action_log`` before the first call, which is what makes the cap
hold across restarts.
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


# --------------------------------------------------------------------------- #
# Per-provider caps — one purse per provider, so the DeepSeek judge cannot spend
# the OpenAI budget and vice versa.
# --------------------------------------------------------------------------- #

DEFAULT_PROVIDER_CAP_USD = 2.0

_provider_cost: dict[str, float] = {}
_provider_tokens: dict[str, int] = {}
_provider_day_utc: str = datetime.now(timezone.utc).date().isoformat()


@dataclass
class ProviderUsage:
    provider: str
    tokens_today: int
    cost_today_usd: float
    cap_usd: float
    remaining_usd: float
    day_utc: str


def provider_cap_env(provider: str) -> str:
    return f"PERSONA_EVAL_DAILY_CAP_USD_{provider.strip().upper()}"


def provider_cap_usd(provider: str) -> float:
    raw = os.environ.get(provider_cap_env(provider), "")
    if not raw.strip():
        return DEFAULT_PROVIDER_CAP_USD
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_PROVIDER_CAP_USD


def _roll_provider_day_locked() -> None:
    global _provider_day_utc
    today = datetime.now(timezone.utc).date().isoformat()
    if today != _provider_day_utc:
        _provider_day_utc = today
        _provider_cost.clear()
        _provider_tokens.clear()


def provider_usage(provider: str) -> ProviderUsage:
    key = provider.strip().lower()
    with _lock:
        _roll_provider_day_locked()
        cap = provider_cap_usd(key)
        cost = _provider_cost.get(key, 0.0)
        return ProviderUsage(
            provider=key,
            tokens_today=_provider_tokens.get(key, 0),
            cost_today_usd=round(cost, 6),
            cap_usd=cap,
            remaining_usd=round(max(0.0, cap - cost), 6),
            day_utc=_provider_day_utc,
        )


def provider_remaining_usd(provider: str) -> float:
    return provider_usage(provider).remaining_usd


def seed_provider_cost(provider: str, cost_today_usd: float) -> ProviderUsage:
    """Replay today's already-persisted spend into the counter.

    Takes the larger of the two figures: the counter may already hold this
    process's own calls, and the ledger may hold earlier processes' calls, and
    neither is allowed to erase the other.
    """
    key = provider.strip().lower()
    with _lock:
        _roll_provider_day_locked()
        _provider_cost[key] = max(_provider_cost.get(key, 0.0), max(0.0, float(cost_today_usd)))
    return provider_usage(key)


def record_provider_usage(provider: str, *, tokens: int, cost_usd: float) -> ProviderUsage:
    key = provider.strip().lower()
    with _lock:
        _roll_provider_day_locked()
        _provider_tokens[key] = _provider_tokens.get(key, 0) + max(0, int(tokens))
        _provider_cost[key] = _provider_cost.get(key, 0.0) + max(0.0, float(cost_usd))
    return provider_usage(key)


def reset_provider_usage_for_tests() -> None:
    global _provider_day_utc
    with _lock:
        _provider_day_utc = datetime.now(timezone.utc).date().isoformat()
        _provider_cost.clear()
        _provider_tokens.clear()


def provider_usage_to_dict(snap: ProviderUsage) -> dict[str, Any]:
    return {
        "provider": snap.provider,
        "tokens_today": snap.tokens_today,
        "cost_today_usd": snap.cost_today_usd,
        "cap_usd": snap.cap_usd,
        "remaining_usd": snap.remaining_usd,
        "day_utc": snap.day_utc,
    }
