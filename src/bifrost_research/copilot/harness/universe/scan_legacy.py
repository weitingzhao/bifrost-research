"""Legacy scan universe adapter — wraps top_scan_symbols."""

from __future__ import annotations

from typing import Any, Protocol

from bifrost_research.copilot.harness import data_sources as ds
from bifrost_research.copilot.harness.policy_schema import LoopPolicy
from bifrost_research.copilot.harness.universe.types import FunnelStep, UniverseResult


class _Connection(Protocol):
    def cursor(self) -> Any: ...


def resolve_scan_legacy(
    conn: _Connection,
    policy: LoopPolicy,
    *,
    limit: int,
) -> UniverseResult:
    flag_filter = policy.flag_filter_str()
    rows = ds.top_scan_symbols(
        conn,
        limit=limit,
        flag_filter=flag_filter,
        min_composite_score=policy.min_composite_score,
        preset=policy.preset,
    )
    symbols: list[str] = []
    meta: dict[str, dict[str, Any]] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        symbols.append(sym)
        meta[sym] = row

    funnel = [
        FunnelStep(
            name="scan_legacy",
            in_count=len(rows),
            out_count=len(symbols),
            filter_summary=(
                f"preset={policy.preset}, flag_filter={flag_filter or 'none'}, "
                f"min_composite={policy.min_composite_score}"
            ),
        )
    ]
    return UniverseResult(
        symbols=symbols,
        row_meta_by_symbol=meta,
        funnel=funnel,
        data_source="scan_legacy",
        universe_mode="scan_legacy",
    )
