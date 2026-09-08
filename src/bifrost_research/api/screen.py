"""Screening routes — the foundation asked a question of the whole universe.

GET /research/screen/coverage?tiers=   — which faces the universe actually has
GET /research/screen?lenses=&require=  — the names that pass, and what they lack

Coverage is the calibration number that used to be measured by hand and written
into a document: how many of the universe's symbols have each face. It moves
every night as the collector widens, so it belongs on a page, not in a snapshot.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.lenses.registry import LENSES
from bifrost_research.lenses.screen import screen

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research/screen", tags=["research-screen"])

# The blueprint's six faces (§3.2), and the lenses that answer each.
FACES: dict[str, tuple[str, ...]] = {
    "trend": ("sepa", "momentum"),
    "volatility": ("iv_rank", "iv_percentile", "vrp", "skew", "term_slope"),
    "positioning": ("opex_pin", "gex_regime", "order_sentiment"),
    "forecast": ("terrain_regime",),
    "validation": ("forecast_path",),
}
FACE_OF = {lens: face for face, lenses in FACES.items() for lens in lenses}
ALL_LENSES: tuple[str, ...] = tuple(lens for lenses in FACES.values() for lens in lenses)
# A symbol "has the option side" when it has a reading on a lens that needs a
# chain — the faces the 575-name universe was widened for.
OPTION_LENSES = ("iv_rank", "iv_percentile", "vrp", "opex_pin", "gex_regime", "order_sentiment")


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _tier_list(tiers: str | None) -> list[str] | None:
    if not tiers:
        return None
    wanted = [t.strip().lower() for t in tiers.split(",") if t.strip()]
    bad = [t for t in wanted if t not in ("resident", "core", "edge")]
    if bad:
        raise HTTPException(status_code=400, detail=f"unknown tier: {', '.join(bad)}")
    return wanted


@router.get("/coverage")
def get_coverage(
    tiers: str | None = Query(None, description="resident,core,edge — default all"),
) -> dict[str, Any]:
    """How much of the universe each face actually covers, right now."""
    # Validate before connecting: a bad tier is the caller's 400, not a 503.
    tier_list = _tier_list(tiers)
    conn = _connect_or_503()
    try:
        result = screen(conn, lenses=ALL_LENSES, tiers=tier_list)
    except Exception as exc:
        logger.exception("coverage screen failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        _close_quietly(conn)

    size = len(result.universe)
    lenses = [
        {
            "lens": lens,
            "label": LENSES[lens].label,
            "face": FACE_OF[lens],
            "read": result.coverage.get(lens),
            "of": size,
            "unscreenable": result.unscreenable.get(lens),
        }
        for lens in ALL_LENSES
    ]
    readable = [lens for lens in ALL_LENSES if lens not in result.unscreenable]
    option_readable = [lens for lens in OPTION_LENSES if lens not in result.unscreenable]
    return _ok(
        {
            "universe": size,
            "tiers": tier_list or ["resident", "core", "edge"],
            "lenses": lenses,
            # The roll-up the calibration cares about: how many names are whole.
            "every_face": sum(1 for r in result.rows if not r.missing),
            "no_option_face": sum(
                1 for r in result.rows if not any(lens in r.readings for lens in option_readable)
            ),
            "screenable_lenses": len(readable),
            "unscreenable": result.unscreenable,
        }
    )


@router.get("")
def get_screen(
    lenses: str = Query(..., description="Comma-separated registry lens ids"),
    require: str | None = Query(
        None, description="lens:band|band,lens:band — a symbol must read in one of the bands"
    ),
    tiers: str | None = Query(None),
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    """The names that pass, each with its readings and the faces it lacks."""
    wanted = [x.strip() for x in lenses.split(",") if x.strip()]
    unknown = [x for x in wanted if x not in LENSES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown lens: {', '.join(unknown)}")
    requirements: dict[str, list[str]] = {}
    for clause in (require or "").split(","):
        if not clause.strip():
            continue
        lens, _, bands = clause.partition(":")
        lens = lens.strip()
        if lens not in LENSES:
            raise HTTPException(status_code=400, detail=f"unknown lens in require: {lens}")
        requirements[lens] = [b.strip() for b in bands.split("|") if b.strip()]

    tier_list = _tier_list(tiers)
    conn = _connect_or_503()
    try:
        result = screen(conn, lenses=wanted, tiers=tier_list, require=requirements or None)
    except Exception as exc:
        logger.exception("screen failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        _close_quietly(conn)

    rows = result.survivors if requirements else result.rows
    return _ok(
        {
            "universe": len(result.universe),
            "lenses": list(result.lenses),
            "require": requirements,
            "survivors": len(rows),
            "unscreenable": result.unscreenable,
            "rows": [
                {
                    "symbol": r.symbol,
                    "readings": {
                        lens: {
                            "value": reading.value,
                            "band": reading.band,
                            "as_of": reading.as_of.isoformat() if reading.as_of else None,
                        }
                        for lens, reading in r.readings.items()
                    },
                    "missing": list(r.missing),
                }
                for r in rows[:limit]
            ],
            "truncated": len(rows) > limit,
        }
    )


__all__ = ["router", "FACES", "ALL_LENSES"]
