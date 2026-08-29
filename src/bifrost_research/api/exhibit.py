"""Analyze Exhibit contract — Wave 15.

GET /research/exhibit/{lens}?symbol=

Lenses: vrp | iv_rank | terrain | order_sentiment
Contract: lens, symbol, as_of, freshness, readings, history_summary, caveats
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from bifrost_research.db.conn import connect
from bifrost_research.schema.schemas import (
    TABLE_OPTION_FLOW_SENTIMENT_DAILY,
    TABLE_OPTION_METRIC_IV_PERCENTILE_DAILY,
    TABLE_STOCK_FORECAST_TERRAIN_DAILY,
    TABLE_STOCK_SIGNAL_VRP_DAILY,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research/exhibit", tags=["research-exhibit"])

LensName = Literal["vrp", "iv_rank", "terrain", "order_sentiment"]
Freshness = Literal["fresh", "stale", "missing"]

_STALE_HOURS = 36.0


class ExhibitResponse(BaseModel):
    lens: str
    symbol: str
    as_of: str | None = None
    freshness: Freshness = "missing"
    readings: dict[str, Any] = Field(default_factory=dict)
    history_summary: dict[str, Any] = Field(default_factory=dict)
    caveats: list[str] = Field(default_factory=list)


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


def _age_hours(ts: Any) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return (datetime.utcnow() - ts).total_seconds() / 3600.0
        return (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 3600.0
    return None


def _freshness_from(ts: Any, has_row: bool) -> Freshness:
    if not has_row:
        return "missing"
    age = _age_hours(ts)
    if age is None:
        return "fresh"  # have a row but no computed_at — treat as present
    return "fresh" if age <= _STALE_HOURS else "stale"


def _iso_date(d: Any) -> str | None:
    if d is None:
        return None
    if isinstance(d, date):
        return d.isoformat()
    return str(d)[:10]


def _exhibit_vrp(conn: Any, symbol: str) -> ExhibitResponse:
    caveats: list[str] = []
    readings: dict[str, Any] = {}
    history: dict[str, Any] = {}
    as_of = None
    computed_at = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT trade_date, vrp_pct_252d, vrp_60d, atm_iv_30d, rv_60d, computed_at
                FROM {TABLE_STOCK_SIGNAL_VRP_DAILY}
                WHERE symbol = %s
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                (symbol,),
            )
            row = cur.fetchone()
            cur.execute(
                f"""
                SELECT COUNT(*)::bigint,
                       AVG(vrp_pct_252d),
                       AVG(vrp_60d)
                FROM {TABLE_STOCK_SIGNAL_VRP_DAILY}
                WHERE symbol = %s AND trade_date >= CURRENT_DATE - INTERVAL '252 days'
                """,
                (symbol,),
            )
            hist = cur.fetchone()
        if row:
            as_of = _iso_date(row[0])
            readings = {
                "vrp_pct_252d": row[1],
                "vrp_60d": row[2],
                "atm_iv_30d": row[3],
                "rv_60d": row[4],
            }
            computed_at = row[5]
        else:
            caveats.append("No VRP rows for symbol")
        if hist:
            history = {
                "days": int(hist[0] or 0),
                "avg_vrp_pct_252d": hist[1],
                "avg_vrp_60d": hist[2],
            }
    except Exception as exc:
        caveats.append(f"VRP query failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
    return ExhibitResponse(
        lens="vrp",
        symbol=symbol,
        as_of=as_of,
        freshness=_freshness_from(computed_at, bool(readings)),
        readings=readings,
        history_summary=history,
        caveats=caveats,
    )


def _exhibit_iv_rank(conn: Any, symbol: str) -> ExhibitResponse:
    caveats: list[str] = []
    readings: dict[str, Any] = {}
    history: dict[str, Any] = {}
    as_of = None
    computed_at = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT trade_date, iv_rank_1y, iv_percentile_1y, iv_current, computed_at
                FROM {TABLE_OPTION_METRIC_IV_PERCENTILE_DAILY}
                WHERE symbol = %s
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                (symbol,),
            )
            row = cur.fetchone()
        if row:
            as_of = _iso_date(row[0])
            readings = {
                "iv_rank_1y": row[1],
                "iv_percentile_1y": row[2],
                "iv_current": row[3],
            }
            computed_at = row[4]
        else:
            caveats.append("No IV percentile rows; consider VRP lens as proxy")
    except Exception as exc:
        caveats.append(f"IV rank query failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
        # Soft fallback: surface VRP pct as proxy reading
        try:
            vrp = _exhibit_vrp(conn, symbol)
            if vrp.readings.get("vrp_pct_252d") is not None:
                readings = {"vrp_pct_252d_proxy": vrp.readings.get("vrp_pct_252d")}
                as_of = vrp.as_of
                caveats.append("Using vrp_pct_252d as iv_rank proxy")
                return ExhibitResponse(
                    lens="iv_rank",
                    symbol=symbol,
                    as_of=as_of,
                    freshness=vrp.freshness,
                    readings=readings,
                    history_summary=vrp.history_summary,
                    caveats=caveats,
                )
        except Exception:
            pass
    return ExhibitResponse(
        lens="iv_rank",
        symbol=symbol,
        as_of=as_of,
        freshness=_freshness_from(computed_at, bool(readings)),
        readings=readings,
        history_summary=history,
        caveats=caveats,
    )


def _exhibit_terrain(conn: Any, symbol: str) -> ExhibitResponse:
    caveats: list[str] = []
    readings: dict[str, Any] = {}
    as_of = None
    computed_at = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT trade_date, regime, pin_score, trend_release, vol_squeeze,
                       tail_risk, expected_close, spot, computed_at
                FROM {TABLE_STOCK_FORECAST_TERRAIN_DAILY}
                WHERE symbol = %s
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                (symbol,),
            )
            row = cur.fetchone()
        if row:
            as_of = _iso_date(row[0])
            readings = {
                "regime": row[1],
                "pin_score": row[2],
                "trend_release": row[3],
                "vol_squeeze": row[4],
                "tail_risk": row[5],
                "expected_close": row[6],
                "spot": row[7],
            }
            computed_at = row[8]
        else:
            caveats.append("No terrain rows for symbol")
    except Exception as exc:
        caveats.append(f"Terrain query failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
    return ExhibitResponse(
        lens="terrain",
        symbol=symbol,
        as_of=as_of,
        freshness=_freshness_from(computed_at, bool(readings)),
        readings=readings,
        history_summary={},
        caveats=caveats,
    )


def _exhibit_order_sentiment(conn: Any, symbol: str) -> ExhibitResponse:
    caveats: list[str] = []
    readings: dict[str, Any] = {}
    as_of = None
    computed_at = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT trade_date, sentiment_score, pcr_volume, pcr_oi,
                       call_notional, put_notional, computed_at
                FROM {TABLE_OPTION_FLOW_SENTIMENT_DAILY}
                WHERE symbol = %s
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                (symbol,),
            )
            row = cur.fetchone()
        if row:
            as_of = _iso_date(row[0])
            readings = {
                "sentiment_score": row[1],
                "pcr_volume": row[2],
                "pcr_oi": row[3],
                "call_notional": row[4],
                "put_notional": row[5],
            }
            computed_at = row[6]
        else:
            caveats.append("No order-flow sentiment rows for symbol")
    except Exception as exc:
        caveats.append(f"Order sentiment query failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
    return ExhibitResponse(
        lens="order_sentiment",
        symbol=symbol,
        as_of=as_of,
        freshness=_freshness_from(computed_at, bool(readings)),
        readings=readings,
        history_summary={},
        caveats=caveats,
    )


_BUILDERS = {
    "vrp": _exhibit_vrp,
    "iv_rank": _exhibit_iv_rank,
    "terrain": _exhibit_terrain,
    "order_sentiment": _exhibit_order_sentiment,
}


def build_exhibit(conn: Any, lens: str, symbol: str) -> ExhibitResponse:
    builder = _BUILDERS.get(lens)
    if builder is None:
        raise ValueError(f"unknown lens: {lens}")
    return builder(conn, symbol.upper())


@router.get("/composite")
def get_exhibit_composite(
    symbol: str = Query(..., min_length=1, max_length=32),
    lenses: str = Query(
        "vrp,iv_rank,terrain,order_sentiment",
        description="Comma-separated lens ids to include",
    ),
) -> dict[str, Any]:
    """Composite regime ribbon — aggregated exhibit lamps for a symbol."""
    sym = symbol.strip().upper()
    valid: set[str] = {"vrp", "iv_rank", "terrain", "order_sentiment"}
    requested = [x.strip() for x in lenses.split(",") if x.strip()]
    ordered = [x for x in requested if x in valid] or list(valid)
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
    lens: LensName,
    symbol: str = Query(..., min_length=1, max_length=32),
) -> dict[str, Any]:
    sym = symbol.strip().upper()
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


__all__ = ["router", "ExhibitResponse", "build_exhibit"]
