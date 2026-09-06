"""Lens registry — the one vocabulary pages, engines, Copilot and the harness read."""

from bifrost_research.lenses.registry import (
    LENSES,
    REGISTRY_VERSION,
    Band,
    LensSpec,
    band_for_score,
    classify,
    decay_lens_ids,
    public_registry,
    scan_flag,
    similar_lens_ids,
    trigger_side,
)

__all__ = [
    "LENSES",
    "REGISTRY_VERSION",
    "Band",
    "LensSpec",
    "band_for_score",
    "classify",
    "decay_lens_ids",
    "public_registry",
    "scan_flag",
    "similar_lens_ids",
    "trigger_side",
]
