"""GET /research/verdicts/{symbol} — what Copilot and the Loop said about a symbol (D4)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from bifrost_research.copilot.agents.symbol_verdicts import build_symbol_verdicts
from bifrost_research.db.conn import connect

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research/verdicts", tags=["research-verdicts"])


@router.get("/{symbol}")
def get_symbol_verdicts(symbol: str) -> dict[str, Any]:
    sym = (symbol or "").strip().upper()
    if not sym or len(sym) > 32:
        raise HTTPException(status_code=400, detail="symbol required")
    try:
        conn = connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    try:
        return {"ok": True, "data": build_symbol_verdicts(conn, sym)}
    except Exception as exc:
        logger.exception("symbol verdicts failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
