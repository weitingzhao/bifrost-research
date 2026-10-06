"""Standard indicator series, signals and signal statistics — W6.

GET /research/indicators/signals        the signal catalog with default parameters
GET /research/indicators/series         one symbol's bars, indicator series and signal sessions
GET /research/indicators/signal-stats   net forward returns after a signal vs the same names' other sessions

Computed on request from ``raw_market.stock_daily``; nothing is stored.
D10 BLOCKED — read-only analysis, no execution path.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.calendar import ny_today
from bifrost_research.db.conn import connect
from bifrost_research.engines.backtest.catalog import evaluation
from bifrost_research.engines.indicators import bollinger, catalog, ema, get_signal, macd, rsi, signal_mask
from bifrost_research.engines.indicators.bars import load_bars
from bifrost_research.engines.indicators.stats import sign_of, signal_sessions
from bifrost_research.engines.signal_stats import DEFAULT_COST_BPS, evaluate
from bifrost_research.repositories.listing_lineage import live_label

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/indicators", tags=["research-indicators"])

MAX_STATS_SYMBOLS = 50
_WARMUP = 120


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


def _close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _r(v: float | None, nd: int = 4) -> float | None:
    return None if v is None else round(v, nd)


def _csv(raw: str | None) -> list[str]:
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


def _params(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="params must be a JSON object") from exc
    if not isinstance(val, dict):
        raise HTTPException(status_code=400, detail="params must be a JSON object")
    return val


def _window(start: date | None, end: date | None, default_days: int) -> tuple[date, date]:
    end = end or ny_today()
    start = start or end - timedelta(days=default_days)
    if start >= end:
        raise HTTPException(status_code=400, detail="start must be before end")
    if (end - start).days > 366 * 10:
        raise HTTPException(status_code=400, detail="window longer than ten years")
    return start, end


@router.get("/signals")
def signals() -> dict[str, Any]:
    return {"ok": True, "data": {"signals": catalog(), "count": len(catalog())}}


@router.get("/series")
def series(
    symbol: str = Query(..., min_length=1, max_length=16),
    start: date | None = None,
    end: date | None = None,
    signals: str | None = Query(None, description="comma-separated signal ids to mark"),
    rsi_period: int = Query(14, ge=2, le=100),
    macd_fast: int = Query(12, ge=2, le=100),
    macd_slow: int = Query(26, ge=3, le=200),
    macd_signal: int = Query(9, ge=2, le=100),
    bb_period: int = Query(20, ge=2, le=200),
    bb_mult: float = Query(2.0, gt=0, le=5),
    ema_lengths: str = Query("20,50,200"),
) -> dict[str, Any]:
    """Bars with RSI, MACD, Bollinger and EMAs, plus the sessions each requested signal fired."""
    s, e = _window(start, end, 730)
    if macd_fast >= macd_slow:
        raise HTTPException(status_code=400, detail="macd_fast must be shorter than macd_slow")
    try:
        lengths = sorted({int(x) for x in _csv(ema_lengths)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="ema_lengths must be integers") from exc
    if any(n < 2 or n > 400 for n in lengths) or len(lengths) > 5:
        raise HTTPException(status_code=400, detail="ema_lengths: up to five integers in 2..400")
    try:
        specs = [get_signal(sid) for sid in _csv(signals)]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    conn = _connect_or_503()
    try:
        warm = max([_WARMUP, *lengths]) * 2
        bars = load_bars(conn, symbol.strip().upper(), s, e, warmup_sessions=warm)
    except Exception as exc:  # noqa: BLE001
        logger.exception("indicator bars read failed")
        raise HTTPException(status_code=503, detail=f"raw_market.stock_daily: {str(exc)[:200]}") from exc
    finally:
        _close(conn)

    closes = [b["close"] for b in bars]
    r = rsi(closes, rsi_period)
    m = macd(closes, macd_fast, macd_slow, macd_signal)
    bb = bollinger(closes, bb_period, bb_mult)
    emas = {str(n): ema(closes, n) for n in lengths}
    masks = {sp.id: signal_mask(closes, sp.id) for sp in specs}
    keep = [i for i, b in enumerate(bars) if s <= b["date"] <= e]
    rows: list[dict[str, Any]] = []
    for i in keep:
        b = bars[i]
        rows.append(
            {
                "date": b["date"].isoformat(),
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b["volume"],
                "rsi": _r(r[i], 2),
                "macd": _r(m["macd"][i]),
                "macd_signal": _r(m["signal"][i]),
                "macd_hist": _r(m["hist"][i]),
                "bb_mid": _r(bb["mid"][i]),
                "bb_upper": _r(bb["upper"][i]),
                "bb_lower": _r(bb["lower"][i]),
                "ema": {k: _r(v[i]) for k, v in emas.items()},
            }
        )
    markers = [
        {
            "date": bars[i]["date"].isoformat(),
            "signal": sp.id,
            "label": sp.label,
            "direction": sp.direction,
            "close": bars[i]["close"],
        }
        for sp in specs
        for i in keep
        if masks[sp.id][i]
    ]
    markers.sort(key=lambda x: x["date"])
    return {
        "ok": True,
        "data": {
            "symbol": symbol.strip().upper(),
            "start": s.isoformat(),
            "end": e.isoformat(),
            "params": {
                "rsi_period": rsi_period,
                "macd": [macd_fast, macd_slow, macd_signal],
                "bb": [bb_period, bb_mult],
                "ema_lengths": lengths,
            },
            "price_basis": "adjusted daily close (raw_market.stock_daily)",
            "bars": rows,
            "markers": markers,
        },
    }


@router.get("/signal-stats")
def signal_stats(
    signal: str = Query(..., min_length=1, max_length=64),
    symbols: str = Query(..., min_length=1),
    params: str | None = Query(None, description="JSON object overriding the signal's defaults"),
    start: date | None = None,
    end: date | None = None,
    horizons: str = Query("5,10,20"),
    move_threshold: float = Query(0.02, ge=0.0, le=0.5),
    cost_bps: float = Query(DEFAULT_COST_BPS, ge=0.0, le=200.0, description="one-way cost, charged on entry and exit"),
) -> dict[str, Any]:
    """Return after a crossing, entered the next session, net of cost, next to the same
    names' other sessions — method in ``engines/signal_stats.py`` (``data.method``)."""
    s, e = _window(start, end, 365 * 5)
    syms = list(dict.fromkeys(x.upper() for x in _csv(symbols)))
    if not syms or len(syms) > MAX_STATS_SYMBOLS:
        raise HTTPException(status_code=400, detail=f"symbols: 1..{MAX_STATS_SYMBOLS}")
    try:
        hs = sorted({int(x) for x in _csv(horizons)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="horizons must be integers") from exc
    if not hs or any(h < 1 or h > 120 for h in hs) or len(hs) > 6:
        raise HTTPException(status_code=400, detail="horizons: up to six integers in 1..120")
    p = _params(params)
    try:
        spec = get_signal(signal)
        warm = spec.warmup(spec.params(p)) * 3
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    conn = _connect_or_503()
    data: dict[str, tuple[list[date], list[float]]] = {}
    close_on: dict[tuple[str, date], float] = {}
    errors: list[str] = []
    try:
        for sym in syms:
            try:
                bars = load_bars(conn, sym, s, e + timedelta(days=int(max(hs) * 1.5) + 10), warmup_sessions=warm)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{sym}: {type(exc).__name__}: {str(exc)[:160]}")
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                continue
            data[sym] = ([b["date"] for b in bars], [b["close"] for b in bars])
            close_on.update(((live_label(sym), b["date"]), b["close"]) for b in bars)
        if errors and not data:
            raise HTTPException(status_code=503, detail="; ".join(errors)[:400])
        # Loaded with three warm-ups before ``start``; a crossing inside the first
        # one is a name listed too recently for the indicator to have settled.
        by_sym = signal_sessions(data, spec.id, p, s, e, warmup_bars=spec.warmup(spec.params(p)))
        stats = evaluate(
            conn,
            by_sym,
            sign=sign_of(spec.id),
            start=s,
            end=e,
            horizons=hs,
            move_threshold=move_threshold,
            cost_bps=cost_bps,
            detail=True,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("indicator signal stats failed")
        raise HTTPException(status_code=503, detail=str(exc)[:300]) from exc
    finally:
        _close(conn)
    for row in stats.get("recent", []):
        row["close"] = close_on.get((row["symbol"], date.fromisoformat(row["date"])))
    out = {
        "signal": {"id": spec.id, "label": spec.label, "direction": spec.direction, "params": spec.params(p)},
        "window": {"start": s.isoformat(), "end": e.isoformat()},
        "move_threshold": move_threshold,
        "cost_bps": cost_bps,
        **stats,
        "errors": errors,
        "symbols": syms,
    }
    return {"ok": True, "data": out, "evaluation": evaluation("indicator_signal")}
