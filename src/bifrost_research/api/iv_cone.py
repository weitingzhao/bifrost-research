"""IV cone HTTP route — one name's constant-maturity ATM IV against its own year.

Routes:
    GET /research/volatility/iv-cone?symbol=PLTR[&as_of=YYYY-MM-DD][&window=252]

See ``repositories.iv_cone`` for how each horizon is read and when its
percentiles are withheld. Plain JSON (no ``{ok, data}`` envelope), like the
other ``/research/volatility/*`` routes.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.repositories import iv_cone as repo

router = APIRouter(prefix="/research", tags=["research-engines"])


@router.get("/volatility/iv-cone")
def iv_cone(
    symbol: str = Query(..., min_length=1, max_length=32),
    as_of: date | None = Query(None, description="Newest session to read (default: latest in the store)"),
    window: int = Query(repo.WINDOW_SESSIONS, ge=repo.MIN_SESSIONS, le=504, description="Sessions in the window"),
) -> dict[str, Any]:
    try:
        conn = connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    try:
        cone = repo.fetch_cone(conn, symbol, as_of=as_of, window_sessions=window)
    finally:
        conn.close()
    if cone["as_of"] is None:
        raise HTTPException(status_code=404, detail="No ATM IV rows for symbol")
    return {**cone, "source": "features.option_metric_atm_iv_daily"}
