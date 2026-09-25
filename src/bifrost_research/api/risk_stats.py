"""Beta, correlation and the realised-vol cone, computed on request (RS2).

GET /analytics/risk/beta?symbols=NVDA,MU&benchmark=SPY&windows=60,252
GET /analytics/risk/correlation?symbols=NVDA,MU,SPY&window=60
GET /analytics/vol/rv-cone?symbol=NVDA&tenors=10,20,30,60,90&years=3
GET /analytics/vol/earnings-moves?symbol=PLTR&limit=8   (research 0.122.0)

Owner chose compute-on-read over a nightly table: the inputs are five years of
``raw_market.stock_daily`` closes, which is small enough to read per request and
saves a table that would need its own freshness story.

Every answer carries the sample it rests on. A window filled under 80% returns
``null`` and its ``n`` — a 252-day beta from 40 sessions is not a 252-day beta.
``as_of`` is the last bar that took part, never today's date.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.engines.volatility.earnings_moves import earnings_moves
from bifrost_research.engines.risk_stats import (
    MIN_FILL,
    aligned_returns,
    beta as beta_of,
    correlation as correlation_of,
    current_realised_vol,
    log_returns,
    rv_cone,
    sufficient,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analytics", tags=["research-risk-stats"])

STOCK_DAILY = "raw_market.stock_daily"
MAX_SYMBOLS = 20
MAX_WINDOW = 1000
MAX_TENOR = 250
MAX_YEARS = 5
# Sessions per calendar day, used to turn a session window into a date filter.
CALENDAR_PER_SESSION = 1.55
DEFAULT_WINDOWS = "60,252"
DEFAULT_TENORS = "10,20,30,60,90"


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _symbols(raw: str, *, limit: int = MAX_SYMBOLS) -> list[str]:
    out: list[str] = []
    for part in raw.split(","):
        sym = part.strip().upper()
        if sym and sym not in out:
            out.append(sym)
    if not out:
        raise HTTPException(status_code=400, detail="symbols is required")
    if len(out) > limit:
        raise HTTPException(status_code=400, detail=f"at most {limit} symbols")
    return out


def _ints(raw: str, *, what: str, maximum: int) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{what} must be integers") from exc
        if value < 2 or value > maximum:
            raise HTTPException(status_code=400, detail=f"{what} must be between 2 and {maximum}")
        if value not in out:
            out.append(value)
    if not out:
        raise HTTPException(status_code=400, detail=f"{what} is required")
    return sorted(out)


def _closes(conn: Any, symbols: list[str], lookback_days: int) -> dict[str, dict[Any, float]]:
    """``{symbol: {bar_date: close}}`` — positive closes only, so a bad row cannot make a return."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT symbol, bar_date, close::float
            FROM {STOCK_DAILY}
            WHERE symbol = ANY(%s)
              AND bar_date >= (CURRENT_DATE - %s * INTERVAL '1 day')
              AND close IS NOT NULL AND close > 0
            ORDER BY symbol, bar_date ASC
            """,
            (symbols, lookback_days),
        )
        rows = cur.fetchall() or []
    out: dict[str, dict[Any, float]] = defaultdict(dict)
    for symbol, bar_date, close in rows:
        out[symbol][bar_date] = float(close)
    return dict(out)


def _sessions_to_days(sessions: int) -> int:
    return int(math.ceil(sessions * CALENDAR_PER_SESSION)) + 10


def _iso(value: Any) -> str | None:
    return None if value is None else str(value)[:10]


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


@router.get("/risk/beta")
def get_beta(
    symbols: str = Query(..., description="Comma-separated symbols"),
    benchmark: str = Query("SPY", min_length=1, max_length=32),
    windows: str = Query(DEFAULT_WINDOWS, description="Comma-separated session windows"),
) -> dict[str, Any]:
    """Beta of each symbol against ``benchmark`` on each window's paired returns."""
    syms = _symbols(symbols)
    bench = benchmark.strip().upper()
    wins = _ints(windows, what="windows", maximum=MAX_WINDOW)
    conn = _connect_or_503()
    try:
        closes = _closes(conn, sorted(set(syms) | {bench}), _sessions_to_days(max(wins)))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    bench_closes = closes.get(bench, {})
    items: list[dict[str, Any]] = []
    as_of: Any = None
    for symbol in syms:
        own = closes.get(symbol, {})
        asset_ret, bench_ret, dates = aligned_returns(own, bench_closes)
        if dates:
            last = dates[-1]
            as_of = last if as_of is None or last > as_of else as_of
        for window in wins:
            a, b = asset_ret[-window:], bench_ret[-window:]
            n = len(a)
            value = beta_of(a, b) if sufficient(n, window) else None
            items.append({"symbol": symbol, "window": window, "beta": value, "n": n})
    return _ok(
        {
            "as_of": _iso(as_of),
            "benchmark": bench,
            "min_fill": MIN_FILL,
            "items": items,
        }
    )


@router.get("/risk/correlation")
def get_correlation(
    symbols: str = Query(..., description=f"Comma-separated symbols (max {MAX_SYMBOLS})"),
    window: int = Query(60, ge=2, le=MAX_WINDOW),
) -> dict[str, Any]:
    """Pairwise return correlation, each pair on its own date intersection."""
    syms = _symbols(symbols)
    conn = _connect_or_503()
    try:
        closes = _closes(conn, syms, _sessions_to_days(window))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    matrix: dict[str, dict[str, Any]] = {s: {} for s in syms}
    as_of: Any = None
    pairs = 0
    for i, left in enumerate(syms):
        for right in syms[i:]:
            l_ret, r_ret, dates = aligned_returns(closes.get(left, {}), closes.get(right, {}))
            l_ret, r_ret = l_ret[-window:], r_ret[-window:]
            n = len(l_ret)
            if dates:
                last = dates[-1]
                as_of = last if as_of is None or last > as_of else as_of
            if left == right:
                cell = {"rho": 1.0 if n else None, "n": n}
            elif sufficient(n, window):
                cell = {"rho": correlation_of(l_ret, r_ret), "n": n}
                pairs += 1
            else:
                cell = {"rho": None, "n": n}
            matrix[left][right] = cell
            matrix[right][left] = cell
    return _ok(
        {
            "as_of": _iso(as_of),
            "window": window,
            "min_fill": MIN_FILL,
            "symbols": syms,
            "matrix": matrix,
            "n_pairs": pairs,
        }
    )


@router.get("/vol/rv-cone")
def get_rv_cone(
    symbol: str = Query(..., min_length=1, max_length=32),
    tenors: str = Query(DEFAULT_TENORS, description="Comma-separated session tenors"),
    years: int = Query(3, ge=1, le=MAX_YEARS),
) -> dict[str, Any]:
    """Percentiles of each tenor's rolling realised vol, and where the name sits today."""
    sym = symbol.strip().upper()
    tenor_list = _ints(tenors, what="tenors", maximum=MAX_TENOR)
    conn = _connect_or_503()
    try:
        closes = _closes(conn, [sym], years * 365)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    series = closes.get(sym, {})
    dates = sorted(series)
    returns = log_returns([series[d] for d in dates])
    rows = rv_cone(returns, tenor_list)
    for row in rows:
        row["current"] = current_realised_vol(returns, int(row["days"]))
    return _ok(
        {
            "as_of": _iso(dates[-1]) if dates else None,
            "symbol": sym,
            "years": years,
            "sessions": len(dates),
            "tenors": rows,
        }
    )


@router.get("/vol/earnings-moves")
def get_earnings_moves(
    symbol: str = Query(..., min_length=1, max_length=32),
    limit: int = Query(8, ge=1, le=20),
) -> dict[str, Any]:
    """Each recent print's straddle-priced move, the move that came, and the IV crush.

    Print dates are the name's 8-K Item 2.02 filings; ``filing_days`` counts every
    day it filed an 8-K at all, so an empty ``prints`` from a name the feed never
    carried reads 0 there, not "no earnings". See ``engines/volatility/earnings_moves``
    for the two-session window and the pricing.
    """
    conn = _connect_or_503()
    try:
        body = earnings_moves(conn, symbol, limit=limit)
    except Exception as exc:
        logger.exception("vol/earnings-moves failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return _ok(body)
