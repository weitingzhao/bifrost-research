"""Similar-regime k-NN — Wave 14 / Analyze B.2 + F.1 lens expansion.

GET /research/similar-regime?lens=...&symbol=&value=&horizon=5&k=5

Lenses
------
- ``vrp`` — k-NN on ``features.stock_signal_vrp_daily.vrp_pct_252d``
- ``iv_rank`` — k-NN on ``option_metric_iv_percentile_daily`` (iv_rank_1y / iv_percentile_1y);
  falls back to VRP if the IV table is empty/unavailable
- ``term_slope`` — k-NN on ``features.option_surface_fit_daily.atm_slope`` for the ~30 DTE
  expiry (row with ``dte`` closest to 30 per trade_date)
- ``pin_distance`` — k-NN on derived pin distance
  ``(close - max_pain) / NULLIF(close, 0)`` from
  ``features.option_metric_max_pain_daily`` joined to ``raw_market.stock_daily``
  (nearest ~30 DTE max-pain expiry per day). Prefer real pct_distance over
  ``dte_to_opex`` proxy.
- ``gex_notional`` — k-NN on ``features.option_metric_gex_levels_daily.total_net_gex``
  for the ~30 DTE expiry per trade_date (also returns ``zero_gamma``, ``spot``)
- ``regime`` — categorical match on ``features.stock_forecast_terrain_daily.regime``
  (exact string, e.g. ``trending`` / ``range`` / ``crash-risk``); ``distance=0`` for all
  matches. ``value`` is the regime string, not a float.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.schema.schemas import (
    TABLE_OPTION_METRIC_GEX_LEVELS_DAILY,
    TABLE_OPTION_METRIC_IV_PERCENTILE_DAILY,
    TABLE_OPTION_METRIC_MAX_PAIN_DAILY,
    TABLE_STOCK_FORECAST_TERRAIN_DAILY,
    TABLE_STOCK_SIGNAL_VRP_DAILY,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research/similar-regime", tags=["research-similar-regime"])

Lens = Literal["vrp", "iv_rank", "term_slope", "pin_distance", "gex_notional", "regime"]

_SURFACE_FIT = "features.option_surface_fit_daily"


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


def parse_numeric_lens_value(value: str) -> float:
    """Parse ``value`` query param for numeric lenses; 400 on invalid input."""
    raw = value.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="value required")
    try:
        return float(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid numeric value for lens: {value!r}",
        ) from exc


def _fwd_return(
    conn: Any,
    symbol: str,
    as_of: date,
    horizon: int,
) -> float | None:
    """Forward N-session return from raw_market.stock_daily if available."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bar_date, close::float
                FROM raw_market.stock_daily
                WHERE symbol = %s
                  AND bar_date >= %s
                  AND close IS NOT NULL AND close > 0
                ORDER BY bar_date ASC
                LIMIT %s
                """,
                (symbol.upper(), as_of, horizon + 1),
            )
            rows = cur.fetchall() or []
        if len(rows) < horizon + 1:
            return None
        c0 = float(rows[0][1])
        c1 = float(rows[horizon][1])
        if c0 <= 0:
            return None
        return (c1 / c0) - 1.0
    except Exception:
        logger.debug("fwd_return unavailable for %s %s", symbol, as_of, exc_info=True)
        return None


def _attach_fwd(
    conn: Any,
    symbol: str,
    raw: list[dict[str, Any]],
    *,
    value: float,
    horizon: int,
    lens_key: str = "lens_value",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw:
        td = item.get("trade_date")
        if isinstance(td, date):
            item["trade_date"] = td.isoformat()
            fwd = _fwd_return(conn, symbol, td, horizon)
        else:
            fwd = None
        lv = item.get(lens_key)
        item["distance"] = abs(float(lv) - value) if lv is not None else None
        item["fwd_return"] = fwd
        if "lens_value" not in item and lv is not None:
            item["lens_value"] = lv
        out.append(item)
    return out


def _similar_vrp(
    conn: Any,
    *,
    symbol: str,
    value: float,
    k: int,
    horizon: int,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT trade_date, symbol, vrp_pct_252d, vrp_60d, atm_iv_30d, rv_60d
            FROM {TABLE_STOCK_SIGNAL_VRP_DAILY}
            WHERE symbol = %s
              AND vrp_pct_252d IS NOT NULL
            ORDER BY ABS(vrp_pct_252d - %s) ASC, trade_date DESC
            LIMIT %s
            """,
            (symbol.upper(), value, k),
        )
        cols = [d[0] for d in cur.description]
        raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    out: list[dict[str, Any]] = []
    for item in raw:
        td = item.get("trade_date")
        if isinstance(td, date):
            item["trade_date"] = td.isoformat()
            fwd = _fwd_return(conn, symbol, td, horizon)
        else:
            fwd = None
        item["distance"] = (
            abs(float(item["vrp_pct_252d"]) - value)
            if item.get("vrp_pct_252d") is not None
            else None
        )
        item["fwd_return"] = fwd
        item["lens_value"] = item.get("vrp_pct_252d")
        out.append(item)
    return out


def _similar_iv_rank(
    conn: Any,
    *,
    symbol: str,
    value: float,
    k: int,
    horizon: int,
) -> tuple[list[dict[str, Any]], str]:
    """Prefer iv_rank_1y / iv_percentile_1y; fall back to vrp_pct_252d."""
    source = "option_metric_iv_percentile_daily"
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT trade_date, symbol,
                       COALESCE(iv_rank_1y, iv_percentile_1y) AS lens_value,
                       iv_rank_1y, iv_percentile_1y, iv_current
                FROM {TABLE_OPTION_METRIC_IV_PERCENTILE_DAILY}
                WHERE symbol = %s
                  AND COALESCE(iv_rank_1y, iv_percentile_1y) IS NOT NULL
                ORDER BY ABS(COALESCE(iv_rank_1y, iv_percentile_1y) - %s) ASC,
                         trade_date DESC
                LIMIT %s
                """,
                (symbol.upper(), value, k),
            )
            cols = [d[0] for d in cur.description]
            raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        logger.info("iv_rank table unavailable; falling back to vrp_pct", exc_info=True)
        return _similar_vrp(conn, symbol=symbol, value=value, k=k, horizon=horizon), "vrp_pct_fallback"

    if not raw:
        return _similar_vrp(conn, symbol=symbol, value=value, k=k, horizon=horizon), "vrp_pct_fallback"

    return _attach_fwd(conn, symbol, raw, value=value, horizon=horizon), source


def _similar_term_slope(
    conn: Any,
    *,
    symbol: str,
    value: float,
    k: int,
    horizon: int,
) -> tuple[list[dict[str, Any]], str]:
    """k-NN on atm_slope for the ~30 DTE surface-fit row per trade_date."""
    source = "option_surface_fit_daily.atm_slope"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH nearest_dte AS (
                SELECT DISTINCT ON (trade_date)
                       trade_date, symbol, atm_slope, dte, expiry
                FROM {_SURFACE_FIT}
                WHERE symbol = %s
                  AND atm_slope IS NOT NULL
                ORDER BY trade_date, ABS(dte - 30) ASC, expiry ASC
            )
            SELECT trade_date, symbol, atm_slope AS lens_value, dte, expiry
            FROM nearest_dte
            ORDER BY ABS(atm_slope - %s) ASC, trade_date DESC
            LIMIT %s
            """,
            (symbol.upper(), value, k),
        )
        cols = [d[0] for d in cur.description]
        raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    for item in raw:
        exp = item.get("expiry")
        if isinstance(exp, date):
            item["expiry"] = exp.isoformat()
    return _attach_fwd(conn, symbol, raw, value=value, horizon=horizon), source


def _similar_pin_distance(
    conn: Any,
    *,
    symbol: str,
    value: float,
    k: int,
    horizon: int,
) -> tuple[list[dict[str, Any]], str]:
    """k-NN on derived pin pct_distance from max_pain × stock close.

    ``pct_distance = (close - max_pain) / NULLIF(close, 0)`` using the ~30 DTE
    max-pain expiry per trade_date. Prefer this over ``dte_to_opex`` proxy.
    """
    source = "max_pain_pin_distance"
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH nearest_mp AS (
                    SELECT DISTINCT ON (m.trade_date)
                           m.trade_date,
                           m.symbol,
                           m.max_pain_strike,
                           m.expiry,
                           (m.expiry - m.trade_date) AS dte
                    FROM {TABLE_OPTION_METRIC_MAX_PAIN_DAILY} m
                    WHERE m.symbol = %s
                      AND m.max_pain_strike IS NOT NULL
                      AND m.max_pain_strike > 0
                    ORDER BY m.trade_date,
                             ABS((m.expiry - m.trade_date) - 30) ASC,
                             m.expiry ASC
                ),
                pin AS (
                    SELECT n.trade_date,
                           n.symbol,
                           n.max_pain_strike,
                           n.expiry,
                           n.dte,
                           s.close::float AS close,
                           (s.close::float - n.max_pain_strike)
                               / NULLIF(s.close::float, 0) AS pct_distance
                    FROM nearest_mp n
                    JOIN raw_market.stock_daily s
                      ON s.symbol = n.symbol
                     AND s.bar_date = n.trade_date
                    WHERE s.close IS NOT NULL
                      AND s.close > 0
                )
                SELECT trade_date, symbol,
                       pct_distance AS lens_value,
                       pct_distance, max_pain_strike, close, dte, expiry
                FROM pin
                WHERE pct_distance IS NOT NULL
                ORDER BY ABS(pct_distance - %s) ASC, trade_date DESC
                LIMIT %s
                """,
                (symbol.upper(), value, k),
            )
            cols = [d[0] for d in cur.description]
            raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        logger.info(
            "pin_distance (max_pain join) unavailable; falling back to dte_to_opex proxy",
            exc_info=True,
        )
        return _similar_dte_to_opex_proxy(
            conn, symbol=symbol, value=value, k=k, horizon=horizon
        )

    if not raw:
        return _similar_dte_to_opex_proxy(
            conn, symbol=symbol, value=value, k=k, horizon=horizon
        )

    for item in raw:
        exp = item.get("expiry")
        if isinstance(exp, date):
            item["expiry"] = exp.isoformat()
    return _attach_fwd(conn, symbol, raw, value=value, horizon=horizon), source


def _similar_dte_to_opex_proxy(
    conn: Any,
    *,
    symbol: str,
    value: float,
    k: int,
    horizon: int,
) -> tuple[list[dict[str, Any]], str]:
    """Fallback proxy: match ABS(dte_to_opex - value) when pin_distance unavailable.

    Documented fallback — prefer real pin_distance from max_pain × close.
    """
    source = "vanna_charm_dte_to_opex_proxy"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, symbol,
                   dte_to_opex::float AS lens_value,
                   dte_to_opex, is_opex_week, spot
            FROM features.option_metric_vanna_charm_daily
            WHERE symbol = %s
              AND dte_to_opex IS NOT NULL
            ORDER BY ABS(dte_to_opex::float - %s) ASC, trade_date DESC
            LIMIT %s
            """,
            (symbol.upper(), value, k),
        )
        cols = [d[0] for d in cur.description]
        raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    return _attach_fwd(conn, symbol, raw, value=value, horizon=horizon), source


def _similar_gex_notional(
    conn: Any,
    *,
    symbol: str,
    value: float,
    k: int,
    horizon: int,
) -> tuple[list[dict[str, Any]], str]:
    """k-NN on total_net_gex for the ~30 DTE GEX levels row per trade_date."""
    source = "option_metric_gex_levels_daily.total_net_gex"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH nearest_expiry AS (
                SELECT DISTINCT ON (trade_date)
                       trade_date, symbol, expiry, spot,
                       total_net_gex, zero_gamma
                FROM {TABLE_OPTION_METRIC_GEX_LEVELS_DAILY}
                WHERE symbol = %s
                  AND total_net_gex IS NOT NULL
                ORDER BY trade_date,
                         ABS((expiry - trade_date) - 30) ASC,
                         expiry ASC
            )
            SELECT trade_date, symbol,
                   total_net_gex AS lens_value,
                   total_net_gex AS gex_notional,
                   zero_gamma, spot, expiry
            FROM nearest_expiry
            ORDER BY ABS(total_net_gex - %s) ASC, trade_date DESC
            LIMIT %s
            """,
            (symbol.upper(), value, k),
        )
        cols = [d[0] for d in cur.description]
        raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    for item in raw:
        exp = item.get("expiry")
        if isinstance(exp, date):
            item["expiry"] = exp.isoformat()
    return _attach_fwd(conn, symbol, raw, value=value, horizon=horizon), source


def _similar_regime_categorical(
    conn: Any,
    *,
    symbol: str,
    regime: str,
    k: int,
    horizon: int,
) -> tuple[list[dict[str, Any]], str]:
    """Exact regime match on terrain daily; distance=0 for all rows."""
    source = "stock_forecast_terrain_daily.regime"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT trade_date, symbol, regime AS lens_value, regime, spot,
                   pin_score, trend_release, vol_squeeze, tail_risk
            FROM {TABLE_STOCK_FORECAST_TERRAIN_DAILY}
            WHERE symbol = %s
              AND regime = %s
            ORDER BY trade_date DESC
            LIMIT %s
            """,
            (symbol.upper(), regime, k),
        )
        cols = [d[0] for d in cur.description]
        raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    out: list[dict[str, Any]] = []
    for item in raw:
        td = item.get("trade_date")
        if isinstance(td, date):
            item["trade_date"] = td.isoformat()
            fwd = _fwd_return(conn, symbol, td, horizon)
        else:
            fwd = None
        item["distance"] = 0.0
        item["fwd_return"] = fwd
        out.append(item)
    return out, source


@router.get("")
def similar_regime(
    lens: Lens = Query("vrp"),
    symbol: str = Query(..., min_length=1, max_length=32),
    value: str = Query(
        ...,
        description="Target lens value — numeric string for k-NN lenses, regime string for regime lens",
    ),
    horizon: int = Query(5, ge=1, le=60),
    k: int = Query(5, ge=1, le=50),
) -> dict[str, Any]:
    sym = symbol.strip().upper()
    conn = _connect_or_503()
    try:
        source: str = TABLE_STOCK_SIGNAL_VRP_DAILY
        response_value: str | float
        if lens == "regime":
            regime_value = value.strip()
            if not regime_value:
                raise HTTPException(status_code=400, detail="regime value required")
            rows, source = _similar_regime_categorical(
                conn, symbol=sym, regime=regime_value, k=k, horizon=horizon
            )
            response_value = regime_value
        else:
            numeric_value = parse_numeric_lens_value(value)
            response_value = numeric_value
            if lens == "vrp":
                rows = _similar_vrp(conn, symbol=sym, value=numeric_value, k=k, horizon=horizon)
            elif lens == "iv_rank":
                rows, source = _similar_iv_rank(
                    conn, symbol=sym, value=numeric_value, k=k, horizon=horizon
                )
            elif lens == "term_slope":
                rows, source = _similar_term_slope(
                    conn, symbol=sym, value=numeric_value, k=k, horizon=horizon
                )
            elif lens == "gex_notional":
                rows, source = _similar_gex_notional(
                    conn, symbol=sym, value=numeric_value, k=k, horizon=horizon
                )
            else:
                rows, source = _similar_pin_distance(
                    conn, symbol=sym, value=numeric_value, k=k, horizon=horizon
                )
        return _ok(
            {
                "lens": lens,
                "symbol": sym,
                "value": response_value,
                "horizon": horizon,
                "k": k,
                "source": source,
                "rows": rows,
                "count": len(rows),
            }
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("similar-regime failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


__all__ = ["router", "Lens", "parse_numeric_lens_value", "_fwd_return"]
