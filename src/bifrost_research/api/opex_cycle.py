"""OpEx Cycle HTTP routes — Wave RS-B-OpEx2.

Response envelope: {"ok": bool, "data": ..., "error"?: str}

Routes:
    GET /research/opex-cycle/current?symbol=SPX
    GET /research/opex-cycle/history?symbol=SPX&cycles=12
    GET /research/opex-cycle/pin-analysis?symbol=SPX&cycles=24
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.engines.opex_cycle.calendar import (
    days_to_opex,
    is_opex_week,
    next_opex_friday,
)
from bifrost_research.repositories import opex_cycle as repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/opex-cycle", tags=["research-opex-cycle"])


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


def _parse_date(s: str | None, name: str) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid {name}: {s}") from exc


@router.get("/current")
def current(
    symbol: str = Query(..., min_length=1, max_length=32),
    trade_date: str | None = Query(None, description="YYYY-MM-DD; latest when omitted"),
    include_map: bool = Query(True, description="Include per-strike Vanna/Charm shape"),
) -> dict[str, Any]:
    td = _parse_date(trade_date, "trade_date")
    conn = _connect_or_503()
    try:
        row = repo.get_current(conn, symbol, trade_date=td)
        strike_map = repo.get_vanna_charm_map(conn, symbol, trade_date=td) if include_map else []
    except Exception as exc:
        logger.exception("opex-cycle/current failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass
    today = date.today()
    return _ok(
        {
            "row": row,
            "strike_map": strike_map,
            "symbol": symbol.strip().upper(),
            "trade_date": td.isoformat() if td else (row.get("trade_date") if row else None),
            "next_opex_date": next_opex_friday(today).isoformat(),
            "dte_to_opex_today": days_to_opex(today),
            "is_opex_week_today": is_opex_week(today),
        }
    )


@router.get("/history")
def history(
    symbol: str = Query(..., min_length=1, max_length=32),
    cycles: int = Query(12, ge=1, le=60),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        rows = repo.get_history(conn, symbol, cycles=cycles)
    except Exception as exc:
        logger.exception("opex-cycle/history failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return _ok(
        {
            "rows": rows,
            "count": len(rows),
            "cycles_requested": cycles,
            "symbol": symbol.strip().upper(),
        }
    )


@router.get("/pin-analysis")
def pin_analysis(
    symbol: str = Query(..., min_length=1, max_length=32),
    cycles: int = Query(24, ge=1, le=60),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        rows = repo.get_pin_analysis(conn, symbol, cycles=cycles)
    except Exception as exc:
        logger.exception("opex-cycle/pin-analysis failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # Compute a coarse "pin risk score": share of cycles where |pct_distance| < 0.5%.
    scored: list[float] = []
    for r in rows:
        pct = r.get("pct_distance")
        if isinstance(pct, (int, float)):
            scored.append(abs(float(pct)))
    pin_rate: float | None = None
    if scored:
        near_pin = sum(1 for x in scored if x < 0.005)
        pin_rate = near_pin / len(scored)
    return _ok(
        {
            "rows": rows,
            "count": len(rows),
            "symbol": symbol.strip().upper(),
            "cycles_requested": cycles,
            "pin_rate": pin_rate,
        }
    )


__all__ = ["router"]
