"""Stock composite funnel — SEPA → Momentum → Events → optional option overlay."""

from __future__ import annotations

from typing import Any, Protocol

from bifrost_research.copilot.harness import readiness as readiness_mod
from bifrost_research.copilot.harness.policy_schema import LoopPolicy, validate_policy_for_mode
from bifrost_research.copilot.harness.universe import events as events_mod
from bifrost_research.copilot.harness.universe import momentum as momentum_mod
from bifrost_research.copilot.harness.universe import option_overlay as overlay_mod
from bifrost_research.copilot.harness.universe import sepa as sepa_mod
from bifrost_research.copilot.harness.universe.types import FunnelStep, UniverseResult


class _Connection(Protocol):
    def cursor(self) -> Any: ...


def _intersect_ordered(base: list[str], other: list[str]) -> list[str]:
    other_set = set(other)
    return [s for s in base if s in other_set]


def _merge_meta(
    target: dict[str, dict[str, Any]],
    source: dict[str, dict[str, Any]],
    symbols: list[str],
) -> dict[str, dict[str, Any]]:
    out = dict(target)
    for sym in symbols:
        if sym in source:
            out[sym] = {**(out.get(sym) or {}), **source[sym]}
    return out


def _apply_layer(
    *,
    name: str,
    current: list[str],
    layer_symbols: list[str],
    required: bool,
    filter_summary: str,
) -> tuple[list[str], FunnelStep]:
    in_count = len(current)
    if not layer_symbols:
        if required:
            step = FunnelStep(
                name=name,
                in_count=in_count,
                out_count=0,
                filter_summary=filter_summary,
                optional=False,
                skipped=False,
            )
            return [], step
        step = FunnelStep(
            name=name,
            in_count=in_count,
            out_count=in_count,
            filter_summary=filter_summary,
            optional=True,
            skipped=True,
            skip_reason="layer returned no symbols",
        )
        return current, step

    if not current:
        out = layer_symbols
    else:
        out = _intersect_ordered(current, layer_symbols)

    dropped = [s for s in current if s not in set(out)] if current else []
    step = FunnelStep(
        name=name,
        in_count=in_count,
        out_count=len(out),
        filter_summary=filter_summary,
        dropped_sample=dropped[:20],
        optional=not required,
    )
    if required and not out and in_count > 0:
        step.skipped = False
    return out, step


def resolve_stock_composite(
    conn: _Connection,
    policy: LoopPolicy,
    *,
    limit: int,
) -> UniverseResult:
    fetch_limit = max(limit * 4, 200)
    funnel: list[FunnelStep] = []
    layer_results: dict[str, Any] = {}
    warnings = validate_policy_for_mode(policy)

    # Layer 1: SEPA (required by default)
    sepa_syms, sepa_meta, sepa_filt = sepa_mod.fetch_sepa_symbols(
        conn, layer=policy.layers.sepa, limit=fetch_limit
    )
    symbols = sepa_syms
    meta = dict(sepa_meta)
    funnel.append(
        FunnelStep(
            name="sepa",
            in_count=len(sepa_syms),
            out_count=len(symbols),
            filter_summary=sepa_filt,
            optional=not policy.layers.sepa.required,
        )
    )
    layer_results["sepa"] = {"count": len(sepa_syms), "filter": sepa_filt}

    # Layer 2: Momentum
    mom_layer = policy.layers.momentum
    mom_syms, mom_meta, mom_filt = momentum_mod.fetch_momentum_symbols(
        conn, layer=mom_layer, limit=fetch_limit
    )
    symbols, mom_step = _apply_layer(
        name="momentum",
        current=symbols,
        layer_symbols=mom_syms,
        required=mom_layer.required,
        filter_summary=mom_filt,
    )
    meta = _merge_meta(meta, mom_meta, symbols)
    funnel.append(mom_step)
    layer_results["momentum"] = {"count": len(mom_syms), "filter": mom_filt}

    # Layer 3: Events
    ev_layer = policy.layers.events
    ev_syms, ev_meta, ev_filt = events_mod.fetch_event_symbols(
        conn, layer=ev_layer, limit=fetch_limit
    )
    symbols, ev_step = _apply_layer(
        name="events",
        current=symbols,
        layer_symbols=ev_syms,
        required=ev_layer.required,
        filter_summary=ev_filt,
    )
    meta = _merge_meta(meta, ev_meta, symbols)
    funnel.append(ev_step)
    layer_results["events"] = {"count": len(ev_syms), "filter": ev_filt}

    # Sort by sepa_score before overlay trim
    def stock_sort_key(sym: str) -> tuple[Any, ...]:
        m = meta.get(sym) or {}
        sepa = m.get("sepa_score")
        mom = m.get("momentum_score") or m.get("score")
        return (
            sepa is None,
            -(float(sepa) if sepa is not None else 0.0),
            mom is None,
            -(float(mom) if mom is not None else 0.0),
            sym,
        )

    symbols.sort(key=stock_sort_key)
    symbols = symbols[:limit]

    overlay_applied = False
    overlay = policy.option_overlay
    if overlay.enabled:
        overlay_ok, overlay_msg = readiness_mod.overlay_readiness(conn, policy)
        if not overlay_ok:
            funnel.append(
                FunnelStep(
                    name="option_overlay",
                    in_count=len(symbols),
                    out_count=len(symbols),
                    filter_summary=overlay_msg,
                    optional=True,
                    skipped=True,
                    skip_reason=overlay_msg,
                )
            )
        else:
            symbols, meta, ov_step, overlay_applied = overlay_mod.apply_option_overlay(
                conn,
                symbols=symbols,
                row_meta=meta,
                overlay=overlay,
                policy=policy,
            )
            if ov_step:
                funnel.append(ov_step)
            symbols = symbols[:limit]

    return UniverseResult(
        symbols=symbols,
        row_meta_by_symbol=meta,
        funnel=funnel,
        data_source="stock_composite",
        universe_mode="stock_composite",
        option_overlay_applied=overlay_applied,
        layer_results=layer_results,
        policy_warnings=warnings,
    )


def resolve_sepa_mode(conn: _Connection, policy: LoopPolicy, *, limit: int) -> UniverseResult:
    syms, meta, filt = sepa_mod.resolve_sepa_only(conn, policy, limit=limit)
    funnel = [FunnelStep(name="sepa", in_count=len(syms), out_count=len(syms), filter_summary=filt)]
    return UniverseResult(
        symbols=syms[:limit],
        row_meta_by_symbol=meta,
        funnel=funnel,
        data_source="sepa",
        universe_mode="sepa",
    )


def resolve_momentum_mode(conn: _Connection, policy: LoopPolicy, *, limit: int) -> UniverseResult:
    syms, meta, filt = momentum_mod.resolve_momentum_only(conn, policy, limit=limit)
    funnel = [
        FunnelStep(name="momentum", in_count=len(syms), out_count=len(syms), filter_summary=filt)
    ]
    return UniverseResult(
        symbols=syms[:limit],
        row_meta_by_symbol=meta,
        funnel=funnel,
        data_source="momentum",
        universe_mode="momentum",
    )


def resolve_events_mode(conn: _Connection, policy: LoopPolicy, *, limit: int) -> UniverseResult:
    syms, meta, filt = events_mod.resolve_events_only(conn, policy, limit=limit)
    funnel = [FunnelStep(name="events", in_count=len(syms), out_count=len(syms), filter_summary=filt)]
    return UniverseResult(
        symbols=syms[:limit],
        row_meta_by_symbol=meta,
        funnel=funnel,
        data_source="events",
        universe_mode="events",
    )
