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
    counts = ds.scan_universe_funnel(
        conn,
        flag_filter=flag_filter,
        min_composite_score=policy.min_composite_score,
    )
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

    # Counted over the whole trade date, not over the LIMITed result — otherwise
    # every step reports `N -> N` and the funnel says nothing about what was
    # dropped.  The last step is the only one the LIMIT applies to.
    funnel = [
        FunnelStep(
            name="scan_universe",
            in_count=counts["total"],
            out_count=counts["with_inputs"],
            filter_summary=(
                f"scan snapshot {counts['trade_date'] or 'n/a'}; "
                f"rows with >= {ds.DEFAULT_MIN_SCAN_INPUTS} scoring input "
                "(a row with none scores the neutral 50 and outranks real scores)"
            ),
        ),
        FunnelStep(
            name="flag_filter",
            in_count=counts["with_inputs"],
            out_count=counts["flag_passed"],
            filter_summary=f"flag_filter={flag_filter or 'none'}",
            optional=not flag_filter,
        ),
        FunnelStep(
            name="min_composite_score",
            in_count=counts["flag_passed"],
            out_count=counts["score_passed"],
            filter_summary=f"min_composite={policy.min_composite_score if policy.min_composite_score is not None else 'unset'}",
            optional=policy.min_composite_score is None,
        ),
        FunnelStep(
            name="top_n",
            in_count=counts["score_passed"],
            out_count=len(symbols),
            filter_summary=f"preset={policy.preset}, limit={limit}",
        ),
    ]
    return UniverseResult(
        symbols=symbols,
        row_meta_by_symbol=meta,
        funnel=funnel,
        data_source="scan_legacy",
        universe_mode="scan_legacy",
    )
