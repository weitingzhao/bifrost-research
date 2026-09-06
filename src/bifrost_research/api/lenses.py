"""Lens registry API — ``GET /research/lenses``.

The Trade frontend reads the same bands the engines apply, so a lab's verdict
label and Signal Decay's hit-rate are about the same "hot". No database.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from bifrost_research.lenses.registry import REGISTRY_VERSION, public_registry, score_bands

router = APIRouter(prefix="/research/lenses", tags=["research-lenses"])


@router.get("")
def list_lenses() -> dict[str, Any]:
    lenses = public_registry()
    return {
        "ok": True,
        "data": {
            "version": REGISTRY_VERSION,
            "score_bands": score_bands(),
            "lenses": lenses,
            "count": len(lenses),
        },
    }
