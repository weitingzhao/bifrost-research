"""Pure helpers for Wave D materialized scanner rows."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def flag_for_score(value: float | None) -> str | None:
    """Map a 0-100 score to sparse hot/cold/neutral flags."""
    if value is None:
        return None
    v = float(value)
    if v >= 80:
        return "hot"
    if v <= 20:
        return "cold"
    if 40 <= v <= 60:
        return "neutral"
    return None


def normalize_atm_slope_score(atm_slope: float | None) -> float | None:
    if atm_slope is None:
        return None
    return 50.0 + _clamp(float(atm_slope) * 200.0, -50.0, 50.0)


def normalize_pin_score(pin_pct: float | None) -> float | None:
    if pin_pct is None:
        return None
    return 50.0 + _clamp(float(pin_pct) * 200.0, -50.0, 50.0)


def compute_composite(parts: dict[str, Any]) -> float | None:
    """Weighted composite from component scores (mostly 0-100)."""
    weights = {
        "iv_rank": 0.25,
        "vrp": 0.25,
        "atm_slope": 0.15,
        "pin": 0.15,
        "terrain": 0.20,
    }

    components: list[tuple[float, float]] = []

    iv_rank = parts.get("iv_rank_1y")
    if iv_rank is not None:
        components.append((float(iv_rank), weights["iv_rank"]))

    vrp = parts.get("vrp_pct_252d")
    if vrp is not None:
        components.append((float(vrp), weights["vrp"]))

    atm_norm = normalize_atm_slope_score(parts.get("atm_slope_30d"))
    if atm_norm is not None:
        components.append((atm_norm, weights["atm_slope"]))

    pin_norm = normalize_pin_score(parts.get("pin_pct_distance"))
    if pin_norm is not None:
        components.append((pin_norm, weights["pin"]))

    pin_score = parts.get("pin_score")
    terrain_score = float(pin_score) if pin_score is not None else 50.0
    components.append((terrain_score, weights["terrain"]))

    if not components:
        return None

    total_weight = sum(weight for _, weight in components)
    if total_weight <= 0:
        return None
    return sum(score * weight for score, weight in components) / total_weight


def build_lens_flags(
    *,
    iv_rank_1y: float | None = None,
    vrp_pct_252d: float | None = None,
    atm_slope_30d: float | None = None,
    pin_pct_distance: float | None = None,
    pin_score: float | None = None,
) -> dict[str, str]:
    flags: dict[str, str] = {}
    if (flag := flag_for_score(iv_rank_1y)) is not None:
        flags["iv_rank"] = flag
    if (flag := flag_for_score(vrp_pct_252d)) is not None:
        flags["vrp"] = flag
    atm_norm = normalize_atm_slope_score(atm_slope_30d)
    if (flag := flag_for_score(atm_norm)) is not None:
        flags["atm_slope"] = flag
    pin_norm = normalize_pin_score(pin_pct_distance)
    if (flag := flag_for_score(pin_norm)) is not None:
        flags["pin"] = flag
    if (flag := flag_for_score(pin_score)) is not None:
        flags["terrain"] = flag
    return flags


def build_scan_row(
    *,
    trade_date: date,
    symbol: str,
    close: float | None = None,
    iv_rank_1y: float | None = None,
    vrp_pct_252d: float | None = None,
    atm_slope_30d: float | None = None,
    pin_pct_distance: float | None = None,
    dte_to_opex: int | None = None,
    zero_gamma_offset: float | None = None,
    gex_notional: float | None = None,
    terrain_regime: str | None = None,
    pin_score: float | None = None,
    tail_risk: float | None = None,
    trend_release: float | None = None,
    computed_at: datetime | None = None,
) -> dict[str, Any]:
    parts = {
        "iv_rank_1y": iv_rank_1y,
        "vrp_pct_252d": vrp_pct_252d,
        "atm_slope_30d": atm_slope_30d,
        "pin_pct_distance": pin_pct_distance,
        "pin_score": pin_score,
    }
    composite = compute_composite(parts)
    lens_flags = build_lens_flags(
        iv_rank_1y=iv_rank_1y,
        vrp_pct_252d=vrp_pct_252d,
        atm_slope_30d=atm_slope_30d,
        pin_pct_distance=pin_pct_distance,
        pin_score=pin_score,
    )
    return {
        "trade_date": trade_date,
        "symbol": symbol.strip().upper(),
        "close": close,
        "iv_rank_1y": iv_rank_1y,
        "vrp_pct_252d": vrp_pct_252d,
        "atm_slope_30d": atm_slope_30d,
        "pin_pct_distance": pin_pct_distance,
        "dte_to_opex": dte_to_opex,
        "zero_gamma_offset": zero_gamma_offset,
        "gex_notional": gex_notional,
        "terrain_regime": terrain_regime,
        "pin_score": pin_score,
        "tail_risk": tail_risk,
        "trend_release": trend_release,
        "composite_score": composite,
        "lens_flags": lens_flags,
        "computed_at": computed_at or datetime.now(timezone.utc),
    }
