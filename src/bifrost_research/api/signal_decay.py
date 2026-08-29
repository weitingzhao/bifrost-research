"""Signal decay / lens hit-rate API — Analyze Waves I / J / L.

GET /research/signal-decay
GET /research/signal-decay/intersect
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.schema.schemas import (
    TABLE_STOCK_FORECAST_TERRAIN_DAILY,
    TABLE_STOCK_SIGNAL_LENS_HIT_DAILY,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/signal-decay", tags=["research-signal-decay"])

Lens = Literal["iv_rank", "vrp", "opex_pin"]
VALID_LENSES = frozenset({"iv_rank", "vrp", "opex_pin"})
VALID_SIDES = frozenset({"hot", "cold"})
VALID_WINDOWS = frozenset({30, 90, 252})
VALID_REGIMES = frozenset({"any", "bull", "rangy", "bear"})


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


def _side_stats(rows: list[dict[str, Any]], side: str) -> dict[str, Any]:
    subset = [r for r in rows if r.get("trigger_side") == side]
    n = len(subset)
    hit5 = [r for r in subset if r.get("hit_5d") is True]
    miss5 = [r for r in subset if r.get("hit_5d") is False]
    eval5 = len(hit5) + len(miss5)
    hit20 = [r for r in subset if r.get("hit_20d") is True]
    miss20 = [r for r in subset if r.get("hit_20d") is False]
    eval20 = len(hit20) + len(miss20)
    return {
        "n": n,
        "hit_5d": len(hit5),
        "evaluated_5d": eval5,
        "pending_5d": n - eval5,
        "hit_rate_5d": round(len(hit5) / eval5, 4) if eval5 else None,
        "hit_20d": len(hit20),
        "evaluated_20d": eval20,
        "pending_20d": n - eval20,
        "hit_rate_20d": round(len(hit20) / eval20, 4) if eval20 else None,
    }


def _rolling_trend(rows: list[dict[str, Any]], *, side: str | None = None) -> list[dict[str, Any]]:
    """Weekly points of rolling hit_rate_5d (calendar week end)."""
    filtered = rows if side is None else [r for r in rows if r.get("trigger_side") == side]
    by_week: dict[str, list[bool]] = {}
    for r in filtered:
        td = r.get("trade_date")
        if not isinstance(td, date):
            continue
        hit = r.get("hit_5d")
        if hit is None:
            continue
        iso = td.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        by_week.setdefault(key, []).append(bool(hit))
    trend: list[dict[str, Any]] = []
    for key in sorted(by_week.keys()):
        vals = by_week[key]
        rate = sum(1 for v in vals if v) / len(vals) if vals else None
        trend.append(
            {
                "week": key,
                "n": len(vals),
                "rolling_hit_rate_5d": round(rate, 4) if rate is not None else None,
            }
        )
    return trend


def _window_slice(raw: list[dict[str, Any]], window_days: int) -> list[dict[str, Any]]:
    dates = [r["trade_date"] for r in raw if isinstance(r.get("trade_date"), date)]
    if not dates:
        return raw
    max_d = max(dates)
    min_keep = max_d - timedelta(days=window_days)
    return [r for r in raw if isinstance(r.get("trade_date"), date) and r["trade_date"] >= min_keep]


def _hit_rates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    hit5 = [r for r in rows if r.get("hit_5d") is True]
    miss5 = [r for r in rows if r.get("hit_5d") is False]
    eval5 = len(hit5) + len(miss5)
    hit20 = [r for r in rows if r.get("hit_20d") is True]
    miss20 = [r for r in rows if r.get("hit_20d") is False]
    eval20 = len(hit20) + len(miss20)
    return {
        "n": len(rows),
        "evaluated_5d": eval5,
        "hit_rate_5d": round(len(hit5) / eval5, 4) if eval5 else None,
        "evaluated_20d": eval20,
        "hit_rate_20d": round(len(hit20) / eval20, 4) if eval20 else None,
    }


def _parse_lens_pairs(raw: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if ":" not in token:
            raise HTTPException(status_code=400, detail=f"invalid lens_pair: {token}")
        lens, side = token.split(":", 1)
        lens = lens.strip().lower()
        side = side.strip().lower()
        if lens not in VALID_LENSES:
            raise HTTPException(status_code=400, detail=f"unsupported lens: {lens}")
        if side not in VALID_SIDES:
            raise HTTPException(status_code=400, detail=f"unsupported side: {side}")
        pairs.append((lens, side))
    if len(pairs) < 2:
        raise HTTPException(status_code=400, detail="lens_pairs requires at least 2 pairs")
    return pairs


def _fetch_lens_rows(
    conn: Any,
    *,
    lens: str,
    cutoff: date,
    symbol: str | None,
    regime: str,
) -> list[dict[str, Any]]:
    """Fetch lens_hit rows; optionally filter by terrain regime (symbol match, SPY fallback)."""
    params: list[Any] = [lens, cutoff]
    where = ["lh.lens = %s", "lh.trade_date >= %s"]
    if symbol:
        where.append("lh.symbol = %s")
        params.append(symbol)

    if regime and regime != "any":
        sql = f"""
            SELECT lh.trade_date, lh.symbol, lh.lens, lh.trigger_side, lh.trigger_value,
                   lh.fwd_return_5d, lh.fwd_return_20d, lh.hit_5d, lh.hit_20d,
                   COALESCE(t_sym.regime, t_spy.regime) AS regime
            FROM {TABLE_STOCK_SIGNAL_LENS_HIT_DAILY} lh
            LEFT JOIN {TABLE_STOCK_FORECAST_TERRAIN_DAILY} t_sym
              ON t_sym.trade_date = lh.trade_date AND t_sym.symbol = lh.symbol
            LEFT JOIN {TABLE_STOCK_FORECAST_TERRAIN_DAILY} t_spy
              ON t_spy.trade_date = lh.trade_date AND t_spy.symbol = 'SPY'
            WHERE {' AND '.join(where)}
              AND LOWER(COALESCE(t_sym.regime, t_spy.regime, '')) = %s
            ORDER BY lh.trade_date ASC
        """
        params.append(regime.lower())
    else:
        sql = f"""
            SELECT lh.trade_date, lh.symbol, lh.lens, lh.trigger_side, lh.trigger_value,
                   lh.fwd_return_5d, lh.fwd_return_20d, lh.hit_5d, lh.hit_20d,
                   NULL::text AS regime
            FROM {TABLE_STOCK_SIGNAL_LENS_HIT_DAILY} lh
            WHERE {' AND '.join(where)}
            ORDER BY lh.trade_date ASC
        """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@router.get("")
def signal_decay(
    lens: Lens = Query("iv_rank"),
    symbol: str | None = Query(None, min_length=1, max_length=32),
    window_days: int = Query(30, ge=1, le=400),
    regime: str = Query("any"),
) -> dict[str, Any]:
    if lens not in VALID_LENSES:
        raise HTTPException(status_code=400, detail=f"unsupported lens: {lens}")
    regime_n = (regime or "any").strip().lower()
    if regime_n not in VALID_REGIMES:
        raise HTTPException(status_code=400, detail=f"unsupported regime: {regime}")

    sym = symbol.strip().upper() if symbol and symbol.strip() else None
    conn = _connect_or_503()
    try:
        cutoff = date.today() - timedelta(days=window_days + 5)
        raw = _fetch_lens_rows(conn, lens=lens, cutoff=cutoff, symbol=sym, regime=regime_n)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("signal_decay query failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass

    raw = _window_slice(raw, window_days)
    by_side = {
        "hot": _side_stats(raw, "hot"),
        "cold": _side_stats(raw, "cold"),
    }
    overall_n = by_side["hot"]["n"] + by_side["cold"]["n"]
    overall_hit5 = by_side["hot"]["hit_5d"] + by_side["cold"]["hit_5d"]
    overall_eval5 = by_side["hot"]["evaluated_5d"] + by_side["cold"]["evaluated_5d"]

    recent: list[dict[str, Any]] = []
    if sym:
        # newest first, last 20 triggers
        ordered = sorted(
            raw,
            key=lambda r: r.get("trade_date") or date.min,
            reverse=True,
        )[:20]
        for r in ordered:
            td = r.get("trade_date")
            recent.append(
                {
                    "trade_date": td.isoformat() if isinstance(td, date) else td,
                    "trigger_side": r.get("trigger_side"),
                    "trigger_value": r.get("trigger_value"),
                    "fwd_return_5d": r.get("fwd_return_5d"),
                    "hit_5d": r.get("hit_5d"),
                    "fwd_return_20d": r.get("fwd_return_20d"),
                    "hit_20d": r.get("hit_20d"),
                    "regime": r.get("regime"),
                }
            )

    return _ok(
        {
            "lens": lens,
            "symbol": sym,
            "window_days": window_days,
            "regime": regime_n,
            "trigger_count": overall_n,
            "hit_rate_5d": round(overall_hit5 / overall_eval5, 4) if overall_eval5 else None,
            "by_side": by_side,
            "trend": _rolling_trend(raw),
            "trend_hot": _rolling_trend(raw, side="hot"),
            "trend_cold": _rolling_trend(raw, side="cold"),
            "recent_triggers": recent,
        }
    )


@router.get("/intersect")
def signal_decay_intersect(
    lens_pairs: str = Query(..., description="Comma pairs e.g. iv_rank:hot,vrp:hot"),
    window_days: int = Query(30, ge=1, le=400),
    symbol: str | None = Query(None, min_length=1, max_length=32),
    regime: str = Query("any"),
) -> dict[str, Any]:
    """Cross-lens intersection hit-rate on (trade_date, symbol)."""
    pairs = _parse_lens_pairs(lens_pairs)
    regime_n = (regime or "any").strip().lower()
    if regime_n not in VALID_REGIMES:
        raise HTTPException(status_code=400, detail=f"unsupported regime: {regime}")
    sym = symbol.strip().upper() if symbol and symbol.strip() else None

    conn = _connect_or_503()
    try:
        cutoff = date.today() - timedelta(days=window_days + 5)
        # Load each lens/side set keyed by (trade_date, symbol)
        per_pair: list[dict[tuple[Any, str], dict[str, Any]]] = []
        baselines: dict[str, Any] = {}
        for lens, side in pairs:
            rows = _fetch_lens_rows(conn, lens=lens, cutoff=cutoff, symbol=sym, regime=regime_n)
            rows = _window_slice(rows, window_days)
            side_rows = [r for r in rows if r.get("trigger_side") == side]
            key_map: dict[tuple[Any, str], dict[str, Any]] = {}
            for r in side_rows:
                td = r.get("trade_date")
                s = str(r.get("symbol") or "").upper()
                if not isinstance(td, date) or not s:
                    continue
                key_map[(td, s)] = r
            per_pair.append(key_map)
            baselines[f"{lens}_{side}"] = _hit_rates(side_rows)

        # Intersection of keys
        keys = set(per_pair[0].keys())
        for m in per_pair[1:]:
            keys &= set(m.keys())

        # Use first pair's hit fields for intersection (same fwd returns per date/symbol)
        intersect_rows = [per_pair[0][k] for k in sorted(keys, key=lambda x: (x[0], x[1]))]
        rates = _hit_rates(intersect_rows)
        sample = []
        for r in sorted(intersect_rows, key=lambda x: x.get("trade_date") or date.min, reverse=True)[:20]:
            td = r.get("trade_date")
            sample.append(
                {
                    "trade_date": td.isoformat() if isinstance(td, date) else td,
                    "symbol": r.get("symbol"),
                    "hit_5d": r.get("hit_5d"),
                    "fwd_return_5d": r.get("fwd_return_5d"),
                }
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("signal_decay intersect failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return _ok(
        {
            "lens_pairs": [f"{l}:{s}" for l, s in pairs],
            "window_days": window_days,
            "symbol": sym,
            "regime": regime_n,
            "n": rates["n"],
            "hit_rate_5d": rates["hit_rate_5d"],
            "hit_rate_20d": rates["hit_rate_20d"],
            "evaluated_5d": rates["evaluated_5d"],
            "evaluated_20d": rates["evaluated_20d"],
            "single_lens_baseline": baselines,
            "sample": sample,
        }
    )


__all__ = ["router", "VALID_LENSES", "_side_stats", "_parse_lens_pairs", "_hit_rates"]
