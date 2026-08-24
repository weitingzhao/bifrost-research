"""Health endpoint for Research API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from bifrost_research import __version__

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "domain": "research",
    }
