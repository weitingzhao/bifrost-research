"""Analyze Exhibit routes — Wave 15, extended by research-loop-automation A2 / C3.

GET /research/exhibit/{lens}?symbol=
GET /research/exhibit/composite?symbol=&lenses=

The builders live in ``bifrost_research.lenses.exhibits`` (one source for the
pages, Copilot and the Daily Brief); this module only serves them.

``composite`` is the batch: the Dossier asks it for all twelve registry lenses
at once. It fans the lenses across a few workers, one connection each, because
opening a connection costs about as much as reading a lens — twelve separate
requests spend that twelve times over.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Sequence

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

# One connection per worker. A connection costs ~0.5s to open and a lens ~0.1-0.8s
# to read, so past four workers the connection is the bill, not the reading.
MAX_COMPOSITE_WORKERS = 4


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


def _stub(lens: str, symbol: str, exc: Exception) -> dict[str, Any]:
    """A lens that failed still answers — as itself, missing, saying why."""
    return ExhibitResponse(
        lens=lens,
        symbol=symbol,
        freshness="missing",
        caveats=[f"lens failed: {exc}"],
    ).model_dump()


def _build_chunk(
    lenses: Sequence[str],
    symbol: str,
    conn: Any = None,
) -> list[tuple[str, dict[str, Any]]]:
    """One worker's share of the lenses, on one connection. Never raises."""
    own = conn is None
    if own:
        try:
            conn = connect()
        except Exception as exc:
            logger.warning("composite worker could not connect: %s", exc)
            return [(lens, _stub(lens, symbol, exc)) for lens in lenses]
    out: list[tuple[str, dict[str, Any]]] = []
    try:
        for lens in lenses:
            try:
                out.append((lens, build_exhibit(conn, lens, symbol).model_dump()))
            except Exception as exc:
                logger.debug("composite lens %s failed: %s", lens, exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                out.append((lens, _stub(lens, symbol, exc)))
    finally:
        if own:
            _close_quietly(conn)
    return out


@router.get("/composite")
def get_exhibit_composite(
    symbol: str = Query(..., min_length=1, max_length=32),
    lenses: str = Query(
        LEGACY_DEFAULT_LENSES,
        description="Comma-separated lens ids to include (any registry lens or legacy alias)",
    ),
) -> dict[str, Any]:
    """Batch exhibits for a symbol — the regime row's lamps, the Dossier's faces."""
    sym = symbol.strip().upper()
    valid = exhibit_lens_names()
    requested = [x.strip() for x in lenses.split(",") if x.strip()]
    ordered = list(
        dict.fromkeys(x for x in requested if x in valid)
    ) or LEGACY_DEFAULT_LENSES.split(",")
    workers = min(MAX_COMPOSITE_WORKERS, len(ordered))
    # Round-robin, so one slow lens does not land in the same chunk as the next.
    chunks = [ordered[i::workers] for i in range(workers)]
    # Probe the database here rather than inside a worker: a database that is
    # down is a 503, not twelve exhibits that each say they failed.
    first = _connect_or_503()
    try:
        if workers == 1:
            pairs = _build_chunk(chunks[0], sym, first)
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="exhibit") as pool:
                futures = [pool.submit(_build_chunk, chunks[0], sym, first)]
                futures += [pool.submit(_build_chunk, chunk, sym) for chunk in chunks[1:]]
                pairs = [pair for future in futures for pair in future.result()]
    finally:
        _close_quietly(first)
    by_lens = dict(pairs)
    return _ok({"symbol": sym, "lenses": ordered, "exhibits": [by_lens[lens] for lens in ordered]})


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
