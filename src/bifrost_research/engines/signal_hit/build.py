"""Pure helpers for lens trigger classification and side-aware hit.

The thresholds live in ``bifrost_research.lenses.registry`` (Phase A1 of
research-loop-automation); these wrappers keep the trigger builder's call sites
and read percentile fractions in [0, 1] as percent, as they always did.
"""

from __future__ import annotations

from bifrost_research.lenses.registry import LENSES, classify_category, trigger_side

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


def classify_skew(atm_slope: float | None) -> str | None:
    """Contrarian on the sign of an extreme skew.

    Call-skew extreme (slope <= -hot) is the hot side and expects the price down;
    put-skew extreme (slope >= +hot) is the cold side and expects it up. Calm and
    lean readings are not triggers.
    """
    if atm_slope is None:
        return None
    if trigger_side("skew", atm_slope) != "hot":
        return None
    return "hot" if float(atm_slope) < 0 else "cold"


def classify_gex_regime(total_net_gex: float | None) -> str | None:
    """Negative net gamma is the hot side (dealers chase), positive the cold side."""
    if total_net_gex is None:
        return None
    regime = "negative" if float(total_net_gex) < 0 else "positive"
    band = classify_category("gex_regime", regime)
    return band if band in ("hot", "cold") else None


def classify_terrain_regime(regime: str | None) -> str | None:
    """Only crash-risk fires (hot); range and trending are not triggers."""
    band = classify_category("terrain_regime", regime)
    return "hot" if band == "hot" else None


def classify_order_sentiment(score: float | None, data_source: str | None) -> str | None:
    """Sentiment triggers only from the trades tape; the OI proxy is not a signal."""
    if data_source != "option_trades_tape":
        return None
    return trigger_side("order_sentiment", score)


def magnitude_hit(*, side: str, fwd_return: float | None, threshold: float) -> bool | None:
    """Hot expects a move at least ``threshold`` in size; cold expects a smaller one."""
    if fwd_return is None:
        return None
    big = abs(float(fwd_return)) >= threshold
    if side == "hot":
        return big
    if side == "cold":
        return not big
    return None


def hit_for(lens_id: str, *, side: str, fwd_return: float | None, horizon: int) -> bool | None:
    """The lens' own hit rule from the registry, for a 5- or 20-session forward return."""
    spec = LENSES[lens_id]
    if spec.hit_rule == "mean_revert":
        return side_aware_hit(side=side, fwd_return=fwd_return)
    if spec.hit_rule == "follow":
        if fwd_return is None:
            return None
        fr = float(fwd_return)
        if side == "hot":
            return fr > 0.0
        if side == "cold":
            return fr < 0.0
        return None
    if spec.hit_rule == "magnitude" and spec.move_threshold is not None:
        threshold = spec.move_threshold[1] if horizon >= 20 else spec.move_threshold[0]
        return magnitude_hit(side=side, fwd_return=fwd_return, threshold=threshold)
    return None


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
