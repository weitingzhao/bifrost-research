"""Pure helpers for lens trigger classification and side-aware hit.

The thresholds live in ``bifrost_research.lenses.registry`` (Phase A1 of
research-loop-automation); these wrappers keep the trigger builder's call sites
and read percentile fractions in [0, 1] as percent, as they always did.
"""

from __future__ import annotations

from bifrost_research.lenses.registry import LENSES, trigger_side

# Kept for callers that still name the numbers; the registry owns them.
HOT_THRESHOLD = LENSES["iv_rank"].bands.hot
COLD_THRESHOLD = LENSES["iv_rank"].bands.cold
OPEX_PIN_HOT_ABS = LENSES["opex_pin"].bands.hot


def classify_iv_rank(value: float | None) -> str | None:
    return trigger_side("iv_rank", value, fractions_as_pct=True)


def classify_vrp(value: float | None) -> str | None:
    return trigger_side("vrp", value, fractions_as_pct=True)


def classify_opex_pin(pin_pct_distance: float | None) -> str | None:
    """Near-pin is hot (mean-revert / pin-converge hypothesis)."""
    return trigger_side("opex_pin", pin_pct_distance)


def side_aware_hit(*, side: str, fwd_return: float | None) -> bool | None:
    """Mean-revert: hot expects negative fwd; cold expects positive fwd."""
    if fwd_return is None:
        return None
    fr = float(fwd_return)
    if side == "hot":
        return fr < 0.0
    if side == "cold":
        return fr > 0.0
    return None
