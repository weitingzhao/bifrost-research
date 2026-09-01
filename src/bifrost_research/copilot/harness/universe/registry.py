"""Universe mode router — Wave LS-2."""

from __future__ import annotations

from typing import Any, Protocol

from bifrost_research.copilot.harness.policy_schema import LoopPolicy, parse_policy
from bifrost_research.copilot.harness.universe import composite as composite_mod
from bifrost_research.copilot.harness.universe import scan_legacy as scan_legacy_mod
from bifrost_research.copilot.harness.universe.types import UniverseResult


class _Connection(Protocol):
    def cursor(self) -> Any: ...


def resolve_universe(
    conn: _Connection,
    policy_raw: dict[str, Any] | LoopPolicy,
    *,
    limit: int,
) -> UniverseResult:
    policy = policy_raw if isinstance(policy_raw, LoopPolicy) else parse_policy(policy_raw)
    mode = policy.universe_mode
    cap = max(1, min(int(limit or 3), 50))

    if mode == "scan_legacy":
        return scan_legacy_mod.resolve_scan_legacy(conn, policy, limit=cap)
    if mode == "sepa":
        return composite_mod.resolve_sepa_mode(conn, policy, limit=cap)
    if mode == "momentum":
        return composite_mod.resolve_momentum_mode(conn, policy, limit=cap)
    if mode == "events":
        return composite_mod.resolve_events_mode(conn, policy, limit=cap)
    # default stock_composite
    return composite_mod.resolve_stock_composite(conn, policy, limit=cap)
