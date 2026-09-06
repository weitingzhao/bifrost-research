"""Analyze Exhibit contract — Wave 15, extended by research-loop-automation A2.

GET /research/exhibit/{lens}?symbol=

Lenses: every id in the lens registry (``GET /research/lenses``), plus the legacy
alias ``terrain`` → ``terrain_regime`` the ribbon still asks for.
Contract: lens, symbol, as_of, freshness, readings, history_summary, caveats,
lens_id, verdict, track_record, similar — the last four from the registry and the
settled record, so a page verdict and a Copilot answer come from one object.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.api.exhibit_lenses import NEW_LENS_BUILDERS, VERDICT_INPUT, enrich_exhibit
from bifrost_research.api.exhibit_model import (
    ExhibitResponse,
    freshness_from,
    iso_date,
)
from bifrost_research.db.conn import connect
from bifrost_research.lenses.registry import LENSES
from bifrost_research.schema.schemas import (
    TABLE_OPTION_FLOW_SENTIMENT_DAILY,
    TABLE_OPTION_METRIC_IV_PERCENTILE_DAILY,
    TABLE_STOCK_FORECAST_TERRAIN_DAILY,
    TABLE_STOCK_SIGNAL_VRP_DAILY,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research/exhibit", tags=["research-exhibit"])

# Wave 15 named the terrain exhibit "terrain"; the registry calls the lens terrain_regime.
LENS_ALIASES: dict[str, str] = {"terrain": "terrain_regime"}
LEGACY_DEFAULT_LENSES = "vrp,iv_rank,terrain,order_sentiment"

_freshness_from = freshness_from
_iso_date = iso_date


def exhibit_lens_names() -> set[str]:
    """Every name ``build_exhibit`` accepts: registry ids and the legacy aliases."""
    return set(LENSES) | set(LENS_ALIASES)


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


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
        lens="terrain_regime",
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
                       call_notional, put_notional, computed_at, data_source
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
                "data_source": row[7],
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
    "terrain_regime": _exhibit_terrain,
    "order_sentiment": _exhibit_order_sentiment,
    **NEW_LENS_BUILDERS,
}


def build_exhibit(conn: Any, lens: str, symbol: str) -> ExhibitResponse:
    """The exhibit for a lens name (registry id or legacy alias), enriched from the registry."""
    lens_id = LENS_ALIASES.get(lens, lens)
    builder = _BUILDERS.get(lens_id)
    if builder is None:
        raise ValueError(f"unknown lens: {lens}")
    exh = builder(conn, symbol.upper())
    exh.lens = lens  # answer with the name that was asked for
    reading_key, fractions = VERDICT_INPUT[lens_id]
    return enrich_exhibit(
        conn,
        exh,
        lens_id=lens_id,
        value=exh.readings.get(reading_key),
        fractions_as_pct=fractions,
    )


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
