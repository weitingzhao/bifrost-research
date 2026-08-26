"""VRP (IV-RV Spread) HTTP routes — Wave RS-B-VRP2.

Response envelope: {"ok": bool, "data": ..., "error"?: str}

Routes:
    GET /research/vrp/latest?symbol=NVDA
    GET /research/vrp/history?symbol=NVDA&days=252
    GET /research/vrp/extremes?bucket=high&limit=20
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.repositories import vrp as repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/vrp", tags=["research-vrp"])


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


@router.get("/latest")
def latest(symbol: str = Query(..., min_length=1, max_length=32)) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        row = repo.get_latest(conn, symbol)
    except Exception as exc:
        logger.exception("vrp/latest failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if row is None:
        return _ok({"row": None, "symbol": symbol.strip().upper()})
    return _ok({"row": row, "symbol": row.get("symbol")})


@router.get("/history")
def history(
    symbol: str = Query(..., min_length=1, max_length=32),
    days: int = Query(252, ge=1, le=5000),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        rows = repo.get_history(conn, symbol, days=days)
    except Exception as exc:
        logger.exception("vrp/history failed")
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
            "days": days,
        }
    )


@router.get("/extremes")
def extremes(
    bucket: str = Query("high", pattern="^(high|low)$"),
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        rows = repo.get_extremes(conn, bucket=bucket, limit=limit)
        as_of = repo.latest_trade_date(conn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("vrp/extremes failed")
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
            "bucket": bucket,
            "limit": limit,
            "as_of": as_of,
        }
    )


__all__ = ["router"]
