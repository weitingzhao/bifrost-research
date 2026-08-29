"""Pure helpers for lens trigger classification and side-aware hit."""

from __future__ import annotations

HOT_THRESHOLD = 80.0
COLD_THRESHOLD = 20.0
OPEX_PIN_HOT_ABS = 0.010  # Wave J: was 0.005 (too sparse — 21 rows / 179d)


def _as_pct(value: float) -> float:
    """Normalize 0-1 fractions into 0-100 percentile scale."""
    v = float(value)
    if 0.0 <= v <= 1.0:
        return v * 100.0
    return v


def classify_iv_rank(value: float | None) -> str | None:
    if value is None:
        return None
    v = _as_pct(value)
    if v >= HOT_THRESHOLD:
        return "hot"
    if v <= COLD_THRESHOLD:
        return "cold"
    return None


def classify_vrp(value: float | None) -> str | None:
    if value is None:
        return None
    v = _as_pct(value)
    if v >= HOT_THRESHOLD:
        return "hot"
    if v <= COLD_THRESHOLD:
        return "cold"
    return None


def classify_opex_pin(pin_pct_distance: float | None) -> str | None:
    """Near-pin is hot (mean-revert / pin-converge hypothesis)."""
    if pin_pct_distance is None:
        return None
    if abs(float(pin_pct_distance)) <= OPEX_PIN_HOT_ABS:
        return "hot"
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
