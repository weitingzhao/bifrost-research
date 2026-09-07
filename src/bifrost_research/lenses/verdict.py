"""Verdict — a reading, its band, and what that band means, from the registry.

The exhibit carries this so a page's verdict strip and Copilot's answer are
built from the same object; neither computes its own threshold.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.lenses.registry import LENSES, classify, classify_category

BAND_LABEL = {
    "hot": "Hot",
    "lean_hot": "Lean hot",
    "neutral": "Neutral",
    "lean_cold": "Lean cold",
    "cold": "Cold",
}

_NO_EDGE = "No standalone edge from this lens — wait for another lens to confirm."


LEAN_HEDGE = "Leaning that way, not at the extreme — "


def verdict_for(
    lens_id: str,
    value: Any,
    *,
    fractions_as_pct: bool = False,
) -> dict[str, Any] | None:
    """``{band, label, value, unit, means}`` or None when the lens has no band for the reading."""
    spec = LENSES[lens_id]
    if spec.kind == "categorical":
        band = classify_category(lens_id, value if value is None else str(value))
    else:
        band = classify(lens_id, value, fractions_as_pct=fractions_as_pct)
    if band is None:
        return None
    if band == "hot":
        means = spec.hot_means
    elif band == "cold":
        means = spec.cold_means
    # A lean band is partway to the extreme, so it must not be handed the extreme's
    # sentence. IV Rank 62 read "Implied vol is rich — short-premium bias", the same
    # words as IV Rank 95, and a reader has no way to see the difference in the prose.
    elif band == "lean_hot":
        means = f"{LEAN_HEDGE}{spec.hot_means}"
    elif band == "lean_cold":
        means = f"{LEAN_HEDGE}{spec.cold_means}"
    elif spec.kind in ("severity", "distance"):
        # Neutral on a severity / distance lens is the calm or far-from-magnet reading.
        means = spec.cold_means or _NO_EDGE
    else:
        means = _NO_EDGE
    return {
        "band": band,
        "label": BAND_LABEL[band],
        "value": value,
        "unit": spec.unit,
        "means": means,
    }
