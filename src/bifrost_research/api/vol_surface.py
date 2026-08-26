"""Vol Surface HTTP routes — Wave RS-B-Surface2.

Response envelope: {"ok": bool, "data": ..., "error"?: str}

Routes:
    GET /research/vol-surface/fit?symbol=NVDA&trade_date=2026-08-25
    GET /research/vol-surface/term-structure?symbol=NVDA&trade_date=2026-08-25
    GET /research/vol-surface/residuals?symbol=NVDA&trade_date=2026-08-25&expiry=2026-09-19
    GET /research/vol-surface/skew-extremes?limit=20
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.repositories import vol_surface as repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/vol-surface", tags=["research-vol-surface"])


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


@router.get("/fit")
def fit(
    symbol: str = Query(..., min_length=1, max_length=32),
    trade_date: str | None = Query(None, description="YYYY-MM-DD; latest when omitted"),
) -> dict[str, Any]:
    td = _parse_date(trade_date, "trade_date")
    conn = _connect_or_503()
    try:
        rows = repo.get_fit(conn, symbol, trade_date=td)
    except Exception as exc:
        logger.exception("vol-surface/fit failed")
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
            "symbol": symbol.strip().upper(),
            "trade_date": td.isoformat() if td else (rows[0].get("trade_date") if rows else None),
        }
    )


@router.get("/term-structure")
def term_structure(
    symbol: str = Query(..., min_length=1, max_length=32),
    trade_date: str | None = Query(None),
) -> dict[str, Any]:
    td = _parse_date(trade_date, "trade_date")
    conn = _connect_or_503()
    try:
        rows = repo.get_term_structure(conn, symbol, trade_date=td)
    except Exception as exc:
        logger.exception("vol-surface/term-structure failed")
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
            "symbol": symbol.strip().upper(),
            "trade_date": td.isoformat() if td else (rows[0].get("trade_date") if rows else None),
        }
    )


@router.get("/residuals")
def residuals(
    symbol: str = Query(..., min_length=1, max_length=32),
    expiry: str = Query(..., description="YYYY-MM-DD"),
    trade_date: str | None = Query(None),
) -> dict[str, Any]:
    exp = _parse_date(expiry, "expiry")
    td = _parse_date(trade_date, "trade_date")
    if exp is None:
        raise HTTPException(status_code=400, detail="expiry is required")
    conn = _connect_or_503()
    try:
        rows = repo.get_residuals(conn, symbol, exp, trade_date=td)
    except Exception as exc:
        logger.exception("vol-surface/residuals failed")
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
            "symbol": symbol.strip().upper(),
            "expiry": exp.isoformat(),
            "trade_date": td.isoformat() if td else (rows[0].get("trade_date") if rows else None),
        }
    )


@router.get("/skew-extremes")
def skew_extremes(
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        rows = repo.get_skew_extremes(conn, limit=limit)
        as_of = repo.latest_trade_date(conn)
    except Exception as exc:
        logger.exception("vol-surface/skew-extremes failed")
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
            "limit": limit,
            "as_of": as_of,
        }
    )


__all__ = ["router"]
