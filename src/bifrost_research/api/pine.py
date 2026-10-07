"""Pine script library and signals — W6.

GET  /research/pine/scripts                 the library (source with ?with_source=true)
GET  /research/pine/scripts/{id}            one script with its source
PUT  /research/pine/scripts/{id}            add or edit a script (owner)
POST /research/pine/check                   run a source over one symbol without saving (owner)
POST /research/pine/try                     run a source over a basket and measure its signals, nothing saved (owner)
GET  /research/pine/context                 the option context series a script can read (S6)
GET  /research/pine/signals                 who fired on a session (Screener), or one symbol's marks (chart)
GET  /research/pine/signal-stats            net forward returns after a script's signal vs the same names' other sessions

Scripts run in the pine-runner (AGPL, its own process, HTTP only).
D10 BLOCKED — signals and statistics only.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from bifrost_research.db.calendar import ny_today
from bifrost_research.auth.deps import require_owner
from bifrost_research.db.conn import connect
from bifrost_research.engines.backtest.catalog import evaluation
from bifrost_research.engines.indicators.bars import load_bars
from bifrost_research.engines.pine import client, context, stats, trial
from bifrost_research.engines.pine.library import (
    MAX_PLOTS,
    PineScript,
    get_script,
    list_scripts,
    upsert_script,
    validate,
)
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


def _problems(message: str, issues: list[dict[str, Any]]) -> JSONResponse:
    """400 with the reason in ``detail`` (what every client reads) and the lines in ``issues``."""
    return JSONResponse(status_code=400, content={"detail": message, "issues": issues})


def _error_issue(res: dict[str, Any]) -> list[dict[str, Any]]:
    """A runner error as one issue when it named the line (runner 0.5.0)."""
    if res.get("line") is None:
        return []
    return [{"line": res["line"], "col": res.get("col") or 1, "message": str(res.get("error"))}]


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
    """A pasted ``source``, or a library ``script`` by id (P1, 0.183.0) — exactly one.

    ``plots`` (P1, G10) asks for those numeric plots' values per session, e.g.
    the Supertrend line the chart draws; the response then carries ``series``.
    """

    model_config = ConfigDict(extra="forbid")
    source: str | None = Field(None, min_length=1, max_length=200_000)
    script: str | None = Field(None, min_length=2, max_length=48)
    symbol: str = Field(..., min_length=1, max_length=16)
    days: int = Field(730, ge=60, le=366 * 6)
    plots: list[Annotated[str, Field(min_length=1, max_length=80)]] | None = Field(None, max_length=MAX_PLOTS)

    @model_validator(mode="after")
    def _one_source(self) -> "CheckBody":
        if (self.source is None) == (self.script is None):
            raise ValueError("send either source or script")
        return self


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
        try:
            issues = client.lint(body.source)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — a script is not saved unchecked
            raise HTTPException(status_code=503, detail=f"the script cannot be checked now (pine-runner): {str(exc)[:200]}") from exc
        if issues:
            first = issues[0]
            return _problems(f"the script has {len(issues)} problem{'' if len(issues) == 1 else 's'}; line {first['line']}: {first['message']}", issues)
        saved = upsert_script(conn, PineScript(id=script_id, **body.model_dump()))
    finally:
        _close(conn)
    return {"ok": True, "data": saved.to_dict()}


@router.get("/context")
def context_series() -> dict[str, Any]:
    """The option context a script reads with ``request.security("NAME", timeframe.period, close)``."""
    return {
        "ok": True,
        "data": {
            "series": [s.to_dict() for s in context.CATALOG.values()],
            "rules": {
                "timeframe": "the script's own (timeframe.period); no higher timeframe yet",
                "missing_day": f"carries the last value for up to {context.FFILL_SESSIONS} sessions, then na",
                "warm_up": f"signals are stored from {context.WARMUP_SESSIONS} sessions after the latest first value among the series a script reads",
                "as_of": "each value is what was known at that session's close",
            },
        },
    }


@router.post("/check", dependencies=[Depends(require_owner)])
def check(body: CheckBody) -> dict[str, Any]:
    """Run a source (or a library script) over one symbol and return its signals — nothing is stored."""
    sym = body.symbol.strip().upper()
    end = ny_today()
    conn = _connect_or_503()
    try:
        lib = get_script(conn, body.script) if body.script else None
        if body.script and lib is None:
            raise HTTPException(status_code=404, detail=f"pine script {body.script} not found")
        source = lib.source if lib is not None else str(body.source)
        try:
            names = context.referenced(source)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        bars = load_bars(conn, sym, end - timedelta(days=body.days), end)
        extra: dict[str, Any] = {}
        warm: date | None = None
        if bars and names:
            extra["context"], extra["market"], warm_by = context.load(conn, names, {sym: bars})
            warm = warm_by.get(sym)
    finally:
        _close(conn)
    if not bars:
        raise HTTPException(status_code=404, detail=f"no daily bars for {sym}")
    try:
        res = client.run(source, {sym: bars}, plots=body.plots, **extra).get(sym, {})
    except client.PineScriptProblems as exc:
        return _problems(str(exc), exc.issues)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"pine-runner unreachable: {str(exc)[:200]}") from exc
    if res.get("error"):
        return _problems(f"script error: {res['error']}", _error_issue(res))
    close_on = {b["date"]: b["close"] for b in bars}
    marks = [
        {"date": d.isoformat(), "side": side, "close": close_on.get(d)}
        for side in ("buy", "sell")
        for d in res.get(side) or []
    ]
    marks.sort(key=lambda m: m["date"])
    data: dict[str, Any] = {"symbol": sym, "bars": len(bars), "marks": marks, "warnings": res.get("warnings") or []}
    if lib is not None:
        data["script"], data["script_version"] = lib.id, lib.version
    if names:
        # The nightly build stores this script's signals from warm_from on (None: never in this window).
        data["context"] = {"series": names, "warm_from": warm.isoformat() if warm else None}
    if body.plots:
        # [[session, value | null]] oldest first; null in the warm-up or where the plot is na.
        data["series"] = {
            title: [[d.isoformat(), v] for d, v in sorted((res.get("series") or {}).get(title, {}).items())]
            for title in body.plots
        }
    return {"ok": True, "data": data}


class TryBody(BaseModel):
    """A pasted ``source`` or a library ``script``, over a basket of names (S13)."""

    model_config = ConfigDict(extra="forbid")
    source: str | None = Field(None, min_length=1, max_length=200_000)
    script: str | None = Field(None, min_length=2, max_length=48)
    symbols: list[Annotated[str, Field(min_length=1, max_length=16)]] | None = Field(None, max_length=trial.MAX_SYMBOLS)
    basket: Literal["resident", "liquid50"] | None = None
    days: int = Field(730, ge=120, le=366 * 6)
    horizons: list[Annotated[int, Field(ge=1, le=120)]] = Field(default_factory=lambda: [5, 10, 20], min_length=1, max_length=6)
    cost_bps: float = Field(stats.DEFAULT_COST_BPS, ge=0.0, le=200.0)

    @model_validator(mode="after")
    def _one_source(self) -> "TryBody":
        if (self.source is None) == (self.script is None):
            raise ValueError("send either source or script")
        if self.symbols and self.basket:
            raise ValueError("send either symbols or basket")
        return self


@router.post("/try", dependencies=[Depends(require_owner)])
def try_script(body: TryBody) -> Any:
    """Run a script over a basket now and measure its signals like Signal Decay — nothing is stored."""
    end = ny_today()
    start = end - timedelta(days=body.days)
    conn = _connect_or_503()
    try:
        lib = get_script(conn, body.script) if body.script else None
        if body.script and lib is None:
            raise HTTPException(status_code=404, detail=f"pine script {body.script} not found")
        source = lib.source if lib is not None else str(body.source)
        try:
            context.referenced(source)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        symbols = body.symbols or trial.basket_symbols(conn, body.basket or trial.DEFAULT_BASKET)
        try:
            out = trial.run_trial(conn, source, symbols, start=start, end=end, horizons=sorted(set(body.horizons)), cost_bps=body.cost_bps)
        except client.PineScriptProblems as exc:
            return _problems(str(exc), exc.issues)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"pine-runner unreachable: {str(exc)[:200]}") from exc
    finally:
        _close(conn)
    data: dict[str, Any] = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "basket": None if body.symbols else (body.basket or trial.DEFAULT_BASKET),
        "horizons": sorted(set(body.horizons)),
        "cost_bps": body.cost_bps,
        **out,
    }
    if lib is not None:
        data["script"], data["script_version"] = lib.id, lib.version
    return {"ok": True, "data": data, "evaluation": evaluation("pine_signal")}


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
                e = end or ny_today()
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


@router.get("/signal-stats")
def signal_stats(
    script: str = Query(..., min_length=2, max_length=48),
    side: Literal["buy", "sell"] = "buy",
    symbols: str | None = Query(None, description="comma-separated; every symbol the script fired on when omitted"),
    start: date | None = None,
    end: date | None = None,
    horizons: str = Query("5,10,20"),
    move_threshold: float = Query(0.02, ge=0.0, le=0.5),
    cost_bps: float = Query(stats.DEFAULT_COST_BPS, ge=0.0, le=200.0, description="one-way cost, charged on entry and exit"),
) -> dict[str, Any]:
    """Return after a script's signal, entered the next session, net of cost, next to the
    same names' other sessions — method in ``engines/signal_stats.py`` (``data.method``)."""
    e = end or ny_today()
    s = start or e - timedelta(days=365 * 5)
    try:
        hs = sorted({int(x) for x in _csv(horizons)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="horizons must be integers") from exc
    if not hs or any(h < 1 or h > 120 for h in hs) or len(hs) > 6:
        raise HTTPException(status_code=400, detail="horizons: up to six integers in 1..120")
    syms = [x.upper() for x in _csv(symbols)]
    conn = _connect_or_503()
    try:
        out = stats.signal_stats(
            conn,
            script=script,
            side=side,
            symbols=syms,
            start=s,
            end=e,
            horizons=hs,
            move_threshold=move_threshold,
            cost_bps=cost_bps,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("pine signal stats failed")
        raise HTTPException(status_code=503, detail=str(exc)[:300]) from exc
    finally:
        _close(conn)
    return {
        "ok": True,
        "data": {
            "script": script,
            "side": side,
            "window": {"start": s.isoformat(), "end": e.isoformat()},
            "symbols": syms,
            "move_threshold": move_threshold,
            "cost_bps": cost_bps,
            **out,
        },
        "evaluation": evaluation("pine_signal"),
    }
