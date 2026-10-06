"""Pine script library and signals — W6.

GET  /research/pine/scripts                 the library (source with ?with_source=true)
GET  /research/pine/scripts/{id}            one script with its source
PUT  /research/pine/scripts/{id}            add or edit a script (owner)
POST /research/pine/check                   run a source over one symbol without saving (owner)
GET  /research/pine/signals                 who fired on a session (Screener), or one symbol's marks (chart)
GET  /research/pine/signal-stats            forward returns after a script's signal vs every session

Scripts run in the pine-runner (AGPL, its own process, HTTP only).
D10 BLOCKED — signals and statistics only.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.auth.deps import require_owner
from bifrost_research.db.conn import connect
from bifrost_research.engines.backtest.catalog import evaluation
from bifrost_research.engines.indicators.bars import load_bars
from bifrost_research.engines.pine import client
from bifrost_research.engines.pine.library import PineScript, get_script, list_scripts, upsert_script, validate
from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_PINE_DAILY

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/pine", tags=["research-pine"])

MAX_SIGNAL_SYMBOLS = 3000


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


def _csv(raw: str | None) -> list[str]:
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


class ScriptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=120)
    source: str = Field(..., min_length=1, max_length=200_000)
    origin: Literal["community", "user"] = "user"
    license: str | None = Field(None, max_length=120)
    source_url: str | None = Field(None, max_length=500)
    notes: str | None = Field(None, max_length=2000)
    is_active: bool = True


class CheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(..., min_length=1, max_length=200_000)
    symbol: str = Field(..., min_length=1, max_length=16)
    days: int = Field(730, ge=60, le=366 * 6)


@router.get("/scripts")
def scripts(with_source: bool = False) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        rows = list_scripts(conn)
        counts: dict[str, dict[str, Any]] = {}
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT script_id, side, COUNT(*), MAX(trade_date)
                    FROM {TABLE_STOCK_SIGNAL_PINE_DAILY} GROUP BY script_id, side"""
            )
            for sid, side, n, last in cur.fetchall() or []:
                c = counts.setdefault(str(sid), {})
                c[f"{side}_signals"] = int(n)
                c["last_signal"] = max(filter(None, [c.get("last_signal"), last.isoformat() if last else None]), default=None)
    except Exception as exc:  # noqa: BLE001
        logger.exception("pine scripts read failed")
        raise HTTPException(status_code=503, detail=str(exc)[:300]) from exc
    finally:
        _close(conn)
    return {
        "ok": True,
        "data": {
            "scripts": [{**s.to_dict(with_source=with_source), **counts.get(s.id, {})} for s in rows],
            "count": len(rows),
        },
    }


@router.get("/scripts/{script_id}")
def script(script_id: str = Path(..., min_length=2, max_length=48)) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        s = get_script(conn, script_id)
    finally:
        _close(conn)
    if s is None:
        raise HTTPException(status_code=404, detail=f"pine script {script_id} not found")
    return {"ok": True, "data": s.to_dict()}


@router.put("/scripts/{script_id}", dependencies=[Depends(require_owner)])
def put_script(body: ScriptBody, script_id: str = Path(..., min_length=2, max_length=48)) -> dict[str, Any]:
    try:
        validate(script_id, body.name, body.source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn = _connect_or_503()
    try:
        current = get_script(conn, script_id)
        if current is not None and current.origin == "bifrost":
            raise HTTPException(
                status_code=409,
                detail="built-in scripts are edited in the repository; save a copy under another id",
            )
        saved = upsert_script(conn, PineScript(id=script_id, **body.model_dump()))
    finally:
        _close(conn)
    return {"ok": True, "data": saved.to_dict()}


@router.post("/check", dependencies=[Depends(require_owner)])
def check(body: CheckBody) -> dict[str, Any]:
    """Run a source over one symbol and return its signals — nothing is stored."""
    sym = body.symbol.strip().upper()
    end = date.today()
    conn = _connect_or_503()
    try:
        bars = load_bars(conn, sym, end - timedelta(days=body.days), end)
    finally:
        _close(conn)
    if not bars:
        raise HTTPException(status_code=404, detail=f"no daily bars for {sym}")
    try:
        res = client.run(body.source, {sym: bars}).get(sym, {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"pine-runner unreachable: {str(exc)[:200]}") from exc
    if res.get("error"):
        raise HTTPException(status_code=400, detail=f"script error: {res['error']}")
    close_on = {b["date"]: b["close"] for b in bars}
    marks = [
        {"date": d.isoformat(), "side": side, "close": close_on.get(d)}
        for side in ("buy", "sell")
        for d in res.get(side) or []
    ]
    marks.sort(key=lambda m: m["date"])
    return {
        "ok": True,
        "data": {"symbol": sym, "bars": len(bars), "marks": marks, "warnings": res.get("warnings") or []},
    }


@router.get("/signals")
def signals(
    script: str | None = Query(None, description="comma-separated script ids; all when omitted"),
    side: Literal["buy", "sell", "any"] = "any",
    on: date | None = Query(None, description="session for the Screener; latest built session when omitted"),
    within_sessions: int = Query(1, ge=1, le=20, description="signals in the last N sessions up to `on`"),
    symbol: str | None = Query(None, max_length=16, description="one symbol's marks over [start, end] instead"),
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    ids = _csv(script)
    where = ["TRUE"]
    args: list[Any] = []
    if ids:
        where.append("script_id = ANY(%s::text[])")
        args.append(ids)
    if side != "any":
        where.append("side = %s")
        args.append(side)
    conn = _connect_or_503()
    try:
        with conn.cursor() as cur:
            if symbol:
                e = end or date.today()
                s = start or e - timedelta(days=730)
                cur.execute(
                    f"""SELECT script_id, symbol, trade_date, side, close FROM {TABLE_STOCK_SIGNAL_PINE_DAILY}
                        WHERE {' AND '.join(where)} AND symbol = %s AND trade_date BETWEEN %s AND %s
                        ORDER BY trade_date""",
                    (*args, symbol.strip().upper(), s, e),
                )
                window = {"symbol": symbol.strip().upper(), "start": s.isoformat(), "end": e.isoformat()}
            else:
                if on is None:
                    cur.execute(f"SELECT MAX(trade_date) FROM {TABLE_STOCK_SIGNAL_PINE_DAILY}")
                    r = cur.fetchone()
                    on = r[0] if r else None
                if on is None:
                    return {"ok": True, "data": {"on": None, "rows": [], "count": 0}}
                cur.execute(
                    """SELECT bar_date FROM raw_market.stock_daily WHERE symbol = 'SPY' AND bar_date <= %s
                       ORDER BY bar_date DESC LIMIT %s""",
                    (on, within_sessions),
                )
                days = [r[0] for r in cur.fetchall() or []]
                lo = min(days) if days else on
                cur.execute(
                    f"""SELECT script_id, symbol, trade_date, side, close FROM {TABLE_STOCK_SIGNAL_PINE_DAILY}
                        WHERE {' AND '.join(where)} AND trade_date BETWEEN %s AND %s
                        ORDER BY trade_date DESC, script_id, symbol
                        LIMIT %s""",
                    (*args, lo, on, MAX_SIGNAL_SYMBOLS * max(1, len(ids) or 8)),
                )
                window = {"on": on.isoformat(), "from": lo.isoformat(), "within_sessions": within_sessions}
            rows = [
                {"script": a, "symbol": b, "date": c.isoformat(), "side": d, "close": e_}
                for a, b, c, d, e_ in cur.fetchall() or []
            ]
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("pine signals read failed")
        raise HTTPException(status_code=503, detail=f"{TABLE_STOCK_SIGNAL_PINE_DAILY}: {str(exc)[:200]}") from exc
    finally:
        _close(conn)
    return {"ok": True, "data": {**window, "rows": rows, "count": len(rows)}}


def _stats_sql(horizons: list[int], with_symbols: bool) -> str:
    leads = ",\n".join(f"LEAD(close, {h}) OVER w AS c{h}" for h in horizons)
    sym_filter = "AND symbol = ANY(%(symbols)s::text[])" if with_symbols else ""
    def ret(h: int) -> str:
        return f"(b.c{h} / b.close - 1)"

    sig_cols = ",\n".join(
        f"""COUNT(b.c{h}) FILTER (WHERE s.symbol IS NOT NULL) AS sn{h},
            AVG(({ret(h)} * %(sign)s > 0)::int) FILTER (WHERE s.symbol IS NOT NULL AND b.c{h} IS NOT NULL) AS sw{h},
            AVG(({ret(h)} * %(sign)s >= %(thr)s)::int) FILTER (WHERE s.symbol IS NOT NULL AND b.c{h} IS NOT NULL) AS sh{h},
            AVG({ret(h)}) FILTER (WHERE s.symbol IS NOT NULL) AS sa{h},
            COUNT(b.c{h}) AS bn{h},
            AVG(({ret(h)} * %(sign)s > 0)::int) FILTER (WHERE b.c{h} IS NOT NULL) AS bw{h},
            AVG(({ret(h)} * %(sign)s >= %(thr)s)::int) FILTER (WHERE b.c{h} IS NOT NULL) AS bh{h},
            AVG({ret(h)}) AS ba{h}"""
        for h in horizons
    )
    return f"""
        WITH s AS (
            SELECT DISTINCT symbol, trade_date FROM {TABLE_STOCK_SIGNAL_PINE_DAILY}
            WHERE script_id = %(script)s AND side = %(side)s
              AND trade_date BETWEEN %(start)s AND %(end)s {sym_filter}
        ),
        b AS (
            SELECT symbol, bar_date, close, {leads}
            FROM raw_market.stock_daily
            WHERE symbol IN (SELECT DISTINCT symbol FROM s) AND bar_date >= %(start)s AND close > 0
            WINDOW w AS (PARTITION BY symbol ORDER BY bar_date)
        )
        SELECT (SELECT COUNT(*) FROM s) AS n_signals, {sig_cols}
        FROM b LEFT JOIN s ON s.symbol = b.symbol AND s.trade_date = b.bar_date
        WHERE b.bar_date <= %(end)s
    """


@router.get("/signal-stats")
def signal_stats(
    script: str = Query(..., min_length=2, max_length=48),
    side: Literal["buy", "sell"] = "buy",
    symbols: str | None = Query(None, description="comma-separated; every symbol the script fired on when omitted"),
    start: date | None = None,
    end: date | None = None,
    horizons: str = Query("5,10,20"),
    move_threshold: float = Query(0.02, ge=0.0, le=0.5),
) -> dict[str, Any]:
    """Win rate and hit rate after a script's signal, next to every session of the same symbols."""
    e = end or date.today()
    s = start or e - timedelta(days=365 * 5)
    try:
        hs = sorted({int(x) for x in _csv(horizons)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="horizons must be integers") from exc
    if not hs or any(h < 1 or h > 120 for h in hs) or len(hs) > 6:
        raise HTTPException(status_code=400, detail="horizons: up to six integers in 1..120")
    syms = [x.upper() for x in _csv(symbols)]
    params = {
        "script": script,
        "side": side,
        "start": s,
        "end": e,
        "symbols": syms,
        "sign": 1 if side == "buy" else -1,
        "thr": move_threshold,
    }
    conn = _connect_or_503()
    try:
        with conn.cursor() as cur:
            cur.execute(_stats_sql(hs, bool(syms)), params)
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
    except Exception as exc:  # noqa: BLE001
        logger.exception("pine signal stats failed")
        raise HTTPException(status_code=503, detail=str(exc)[:300]) from exc
    finally:
        _close(conn)
    r = dict(zip(cols, row or []))

    def _f(v: Any, nd: int = 4) -> float | None:
        return None if v is None else round(float(v), nd)

    by_h: dict[str, Any] = {}
    for h in hs:
        sig = {"n": int(r.get(f"sn{h}") or 0), "win_rate": _f(r.get(f"sw{h}")), "hit_rate": _f(r.get(f"sh{h}")), "avg_return": _f(r.get(f"sa{h}"), 5)}
        base = {"n": int(r.get(f"bn{h}") or 0), "win_rate": _f(r.get(f"bw{h}")), "hit_rate": _f(r.get(f"bh{h}")), "avg_return": _f(r.get(f"ba{h}"), 5)}
        edge = round(sig["win_rate"] - base["win_rate"], 4) if sig["win_rate"] is not None and base["win_rate"] is not None else None
        by_h[str(h)] = {"signal": sig, "baseline": base, "win_rate_edge": edge}
    n = int(r.get("n_signals") or 0)
    return {
        "ok": True,
        "data": {
            "script": script,
            "side": side,
            "window": {"start": s.isoformat(), "end": e.isoformat()},
            "symbols": syms,
            "move_threshold": move_threshold,
            "signals": n,
            "sample_note": "noise" if n < 5 else ("thin" if n < 30 else "ok"),
            "by_horizon": by_h,
        },
        "evaluation": evaluation("pine_signal"),
    }
