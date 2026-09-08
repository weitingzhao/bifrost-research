"""Option overlay — optional scan composite boost on stock universe.

The readings a candidate carries come from the lens layer (``lenses.screen``),
not from the scan table's parallel copy: the same registry bands as the page
the card links to, so the harness and the Workbench cannot disagree about what
a symbol's IV rank is (C-A1). Each symbol also carries the option faces it
does **not** have, so judgement can say "not measured" rather than quietly
weighing a name on fewer faces than it thinks (C-A2).

What decides who passes is unchanged: the scan composite and the policy's
``min_composite``. Re-pointing that gate at lens bands would change what an
Owner-facing policy key means, which is a decision, not a refactor.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from bifrost_research.copilot.harness import data_sources as ds
from bifrost_research.copilot.harness.policy_schema import LoopPolicy, OptionOverlayPolicy
from bifrost_research.copilot.harness.universe.types import FunnelStep
from bifrost_research.lenses.screen import screen

logger = logging.getLogger(__name__)

# The option faces a candidate should carry — one per lens the option side has.
OVERLAY_LENSES = ("iv_rank", "iv_percentile", "vrp", "gex_regime", "opex_pin", "terrain_regime")


class _Connection(Protocol):
    def cursor(self) -> Any: ...


def option_faces(conn: _Connection, symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Per symbol: each option face's reading and band, and the faces it lacks.

    Fails soft — an overlay that cannot read the lens layer still hands back the
    stock universe, with every face recorded as unread rather than as absent.
    """
    try:
        result = screen(conn, lenses=OVERLAY_LENSES, symbols=symbols)
    except Exception as exc:  # noqa: BLE001 — the funnel must not die on a reading
        logger.warning("option overlay could not read the lens layer: %s", exc)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in result.rows:
        out[row.symbol] = {
            "option_faces": {
                lens: {"value": r.value, "band": r.band, "as_of": r.as_of}
                for lens, r in row.readings.items()
            },
            "option_faces_missing": list(row.missing),
        }
    return out


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
    min_comp = (
        overlay.min_composite if overlay.min_composite is not None else policy.min_composite_score
    )

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

    # The lens layer answers for every kept name at once, whatever the scan said.
    faces = option_faces(conn, kept) if kept else {}
    for sym in kept:
        if sym in faces:
            merged_meta[sym] = {**(merged_meta.get(sym) or {}), **faces[sym]}

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
            f"min_composite={min_comp}; "
            f"option faces read for {sum(1 for s in kept if faces.get(s, {}).get('option_faces'))}"
            f"/{len(kept)}"
        ),
        dropped_sample=dropped[:20],
        optional=not overlay.required,
    )
    return kept, merged_meta, step, True
