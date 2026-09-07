"""Analyze Exhibit routes — Wave 15, extended by research-loop-automation A2 / C3.

GET /research/exhibit/{lens}?symbol=
GET /research/exhibit/composite?symbol=&lenses=

The builders live in ``bifrost_research.lenses.exhibits`` (one source for the
pages, Copilot and the Daily Brief); this module only serves them.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.lenses.exhibit_model import ExhibitResponse
from bifrost_research.lenses.exhibits import (  # noqa: F401 — re-exported for callers of the old home
    LEGACY_DEFAULT_LENSES,
    LENS_ALIASES,
    build_exhibit,
    exhibit_lens_names,
    fwd20_by_band,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research/exhibit", tags=["research-exhibit"])


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


@router.get("/composite")
def get_exhibit_composite(
    symbol: str = Query(..., min_length=1, max_length=32),
    lenses: str = Query(
        LEGACY_DEFAULT_LENSES,
        description="Comma-separated lens ids to include (any registry lens or legacy alias)",
    ),
) -> dict[str, Any]:
    """Composite regime ribbon — aggregated exhibit lamps for a symbol."""
    sym = symbol.strip().upper()
    valid = exhibit_lens_names()
    requested = [x.strip() for x in lenses.split(",") if x.strip()]
    ordered = [x for x in requested if x in valid] or LEGACY_DEFAULT_LENSES.split(",")
    conn = _connect_or_503()
    try:
        exhibits: list[dict[str, Any]] = []
        for lens in ordered:
            try:
                exhibits.append(build_exhibit(conn, lens, sym).model_dump())
            except Exception as exc:
                logger.debug("composite lens %s failed: %s", lens, exc)
                exhibits.append(
                    ExhibitResponse(
                        lens=lens,
                        symbol=sym,
                        freshness="missing",
                        caveats=[f"lens failed: {exc}"],
                    ).model_dump()
                )
        return _ok({"symbol": sym, "lenses": ordered, "exhibits": exhibits})
    except Exception as exc:
        logger.exception("exhibit composite failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("/{lens}")
def get_exhibit(
    lens: str,
    symbol: str = Query(..., min_length=1, max_length=32),
) -> dict[str, Any]:
    sym = symbol.strip().upper()
    if lens not in exhibit_lens_names():
        raise HTTPException(status_code=400, detail=f"unknown lens: {lens}")
    conn = _connect_or_503()
    try:
        exhibit = build_exhibit(conn, lens, sym)
        return _ok(exhibit.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("exhibit failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


__all__ = ["router", "ExhibitResponse", "build_exhibit", "exhibit_lens_names", "LENS_ALIASES"]
