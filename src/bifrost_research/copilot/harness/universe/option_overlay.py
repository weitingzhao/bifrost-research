"""Option overlay — optional scan composite boost on stock universe."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from bifrost_research.copilot.harness import data_sources as ds
from bifrost_research.copilot.harness.policy_schema import LoopPolicy, OptionOverlayPolicy
from bifrost_research.copilot.harness.universe.types import FunnelStep

logger = logging.getLogger(__name__)


class _Connection(Protocol):
    def cursor(self) -> Any: ...


def apply_option_overlay(
    conn: _Connection,
    *,
    symbols: list[str],
    row_meta: dict[str, dict[str, Any]],
    overlay: OptionOverlayPolicy,
    policy: LoopPolicy,
) -> tuple[list[str], dict[str, dict[str, Any]], FunnelStep | None, bool]:
    """Apply option scan overlay. When required=false, missing scan rows are kept."""
    if not overlay.enabled or not symbols:
        return symbols, row_meta, None, False

    flag = overlay.flag_filter or policy.flag_filter_str()
    min_comp = overlay.min_composite if overlay.min_composite is not None else policy.min_composite_score

    scan_rows = ds.top_scan_symbols(
        conn,
        limit=max(len(symbols) * 2, 100),
        flag_filter=flag,
        min_composite_score=min_comp if overlay.required else None,
        preset=overlay.scan_preset or policy.preset,
    )
    scan_by_sym = {
        str(r.get("symbol") or "").strip().upper(): r for r in scan_rows if r.get("symbol")
    }

    in_count = len(symbols)
    kept: list[str] = []
    dropped: list[str] = []
    merged_meta = dict(row_meta)

    for sym in symbols:
        scan_row = scan_by_sym.get(sym)
        if scan_row is None:
            if overlay.required:
                dropped.append(sym)
                continue
            kept.append(sym)
            continue
        if overlay.required and min_comp is not None:
            comp = scan_row.get("composite_score")
            if comp is None or float(comp) < float(min_comp):
                dropped.append(sym)
                continue
        kept.append(sym)
        base = merged_meta.get(sym) or {}
        merged_meta[sym] = {
            **base,
            "option_composite": scan_row.get("composite_score"),
            "iv_rank_1y": scan_row.get("iv_rank_1y"),
            "vrp_pct_252d": scan_row.get("vrp_pct_252d"),
            "lens_flags": scan_row.get("lens_flags"),
            "terrain_regime": scan_row.get("terrain_regime"),
        }

    if overlay.required and not kept and symbols:
        logger.warning("option overlay required removed all symbols; fail-soft keep stock set")
        kept = symbols
        dropped = []

    def sort_key(sym: str) -> tuple[Any, ...]:
        meta = merged_meta.get(sym) or {}
        opt = meta.get("option_composite")
        sepa = meta.get("sepa_score")
        return (
            opt is None,
            -(float(opt) if opt is not None else 0.0),
            sepa is None,
            -(float(sepa) if sepa is not None else 0.0),
            sym,
        )

    kept.sort(key=sort_key)

    step = FunnelStep(
        name="option_overlay",
        in_count=in_count,
        out_count=len(kept),
        filter_summary=(
            f"enabled required={overlay.required} flag={flag or 'none'} "
            f"min_composite={min_comp}"
        ),
        dropped_sample=dropped[:20],
        optional=not overlay.required,
    )
    return kept, merged_meta, step, True
