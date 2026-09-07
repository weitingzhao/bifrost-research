"""Exhibit builders — one reader per lens, enriched from the registry.

``build_exhibit(conn, lens, symbol)`` is the single source a hub view's verdict
strip, the Copilot ``research.exhibit.get`` tool and (since research-loop-automation
C3) the Daily Brief all read. It lives under ``lenses`` so engines can call it
without importing the HTTP layer; ``api/exhibit.py`` is only the router.

Contract: lens, symbol, as_of, freshness, readings, history_summary, caveats,
lens_id, verdict, track_record, similar.
"""

from __future__ import annotations

import logging
from typing import Any

from bifrost_research.lenses.exhibit_lenses import NEW_LENS_BUILDERS, VERDICT_INPUT, enrich_exhibit
from bifrost_research.lenses.exhibit_model import ExhibitResponse, freshness_from, iso_date
from bifrost_research.lenses.registry import LENSES
from bifrost_research.schema.schemas import (
    TABLE_OPTION_FLOW_SENTIMENT_DAILY,
    TABLE_OPTION_METRIC_IV_PERCENTILE_DAILY,
    TABLE_STOCK_FORECAST_TERRAIN_DAILY,
    TABLE_STOCK_SIGNAL_VRP_DAILY,
)

logger = logging.getLogger(__name__)

# Wave 15 named the terrain exhibit "terrain"; the registry calls the lens terrain_regime.
LENS_ALIASES: dict[str, str] = {"terrain": "terrain_regime"}
LEGACY_DEFAULT_LENSES = "vrp,iv_rank,terrain,order_sentiment"

def exhibit_lens_names() -> set[str]:
    """Every name ``build_exhibit`` accepts: registry ids and the legacy aliases."""
    return set(LENSES) | set(LENS_ALIASES)


def fwd20_by_band(row: Any) -> dict[str, Any]:
    """Shape the six-column aggregate into ``{hot, cold}`` records."""

    def _side(n: Any, median: Any, share: Any) -> dict[str, Any]:
        count = int(n or 0)
        return {
            "n": count,
            "median_fwd": float(median) if count and median is not None else None,
            "share_positive": float(share) if count and share is not None else None,
        }

    return {
        "horizon": 20,
        "hot": _side(row[0], row[1], row[2]),
        "cold": _side(row[3], row[4], row[5]),
        "hot_band": ">= 80",
        "cold_band": "<= 20",
    }


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
            # C2: what happened 20 sessions after this symbol's own hot / cold
            # readings — the lab's record, from the fwd_ret_20d column A4 filled.
            cur.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE vrp_pct_252d >= 80),
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY fwd_ret_20d)
                        FILTER (WHERE vrp_pct_252d >= 80),
                    AVG((fwd_ret_20d > 0)::int) FILTER (WHERE vrp_pct_252d >= 80),
                    COUNT(*) FILTER (WHERE vrp_pct_252d <= 20),
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY fwd_ret_20d)
                        FILTER (WHERE vrp_pct_252d <= 20),
                    AVG((fwd_ret_20d > 0)::int) FILTER (WHERE vrp_pct_252d <= 20)
                FROM {TABLE_STOCK_SIGNAL_VRP_DAILY}
                WHERE symbol = %s AND fwd_ret_20d IS NOT NULL AND vrp_pct_252d IS NOT NULL
                  AND trade_date >= CURRENT_DATE - INTERVAL '504 days'
                """,
                (symbol,),
            )
            fwd = cur.fetchone()
        if row:
            as_of = iso_date(row[0])
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
        if fwd:
            history["fwd20_by_band"] = fwd20_by_band(fwd)
            if not history["fwd20_by_band"]["hot"]["n"] and not history["fwd20_by_band"]["cold"]["n"]:
                caveats.append("No settled 20-session forward returns for hot / cold readings yet")
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
        freshness=freshness_from(computed_at, bool(readings)),
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
            as_of = iso_date(row[0])
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
        freshness=freshness_from(computed_at, bool(readings)),
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
            as_of = iso_date(row[0])
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
        freshness=freshness_from(computed_at, bool(readings)),
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
            as_of = iso_date(row[0])
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
        freshness=freshness_from(computed_at, bool(readings)),
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
