"""Wave 4 research engine APIs — forecast / event radar / backtest.

Compute endpoints are in-memory (no DB required). List/read endpoints
query research.* when golden_source is available.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from bifrost_research.db.conn import connect
from bifrost_research.engines.backtest.settlement import (
    aggregate_accuracy,
    settle_forecast,
)
from bifrost_research.engines.event_radar.pipeline import run_pipeline
from bifrost_research.engines.forecast.llm import get_default_provider
from bifrost_research.engines.forecast.terrain import compute_market_terrain
from bifrost_research.engines.forecast.playbook import build_forecast_session
from bifrost_research.engines.backtest.regime_stats import compute_regime_stats
from bifrost_research.engines.brief.synth import synthesize_daily_brief

router = APIRouter(prefix="/research", tags=["research-wave4"])


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()[:10]
    if not s:
        return None
    return date.fromisoformat(s)


def _row_dict(row: Any, columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        out = dict(row)
    else:
        out = {columns[i]: row[i] for i in range(min(len(columns), len(row)))}
    for key in ("trade_date", "collected_at", "period_start", "period_end"):
        if key in out and out[key] is not None:
            d = _as_date(out[key])
            out[key] = d.isoformat() if d else out[key]
    if "computed_at" in out and isinstance(out["computed_at"], datetime):
        out["computed_at"] = out["computed_at"].isoformat()
    return out


def _extract_hourly_realized(stats_json: Any) -> list[dict[str, Any]] | None:
    """Pull realized hourly closes from settlement ``stats_json.hourly_close``."""
    if not isinstance(stats_json, dict):
        return None
    hourly_close = stats_json.get("hourly_close")
    if not isinstance(hourly_close, list) or not hourly_close:
        return None
    out: list[dict[str, Any]] = []
    for item in hourly_close:
        if isinstance(item, dict):
            out.append(dict(item))
    return out or None


def _enrich_settlement_row(row: dict[str, Any]) -> dict[str, Any]:
    """Attach ``hourly_realized`` from stats_json; ``hourly_json`` stays forecast path."""
    row["hourly_realized"] = _extract_hourly_realized(row.get("stats_json"))
    return row


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


# ---------------------------------------------------------------------------
# 6.2 Intraday Terrain / GEX
# ---------------------------------------------------------------------------


@router.get("/terrain/intraday")
def terrain_intraday(
    symbol: str = Query(...),
    date_param: date | None = Query(None, alias="date"),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "symbol",
        "trade_date",
        "asof_ts",
        "pin_score",
        "trend_release",
        "vol_squeeze",
        "tail_risk",
        "expected_close",
        "gamma_zone_low",
        "gamma_zone_high",
        "regime",
        "spot",
        "prob_rangy",
        "prob_bull",
        "prob_bear",
        "prob_squeeze",
        "inputs_json",
        "computed_at",
    )
    sym = symbol.strip().upper()
    try:
        resolved = date_param
        if resolved is None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(trade_date) FROM features.stock_forecast_terrain_intraday WHERE symbol = %s",
                    (sym,),
                )
                row = cur.fetchone()
            if row is not None:
                resolved = _as_date(
                    row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
                )
        if resolved is None:
            return {
                "rows": [],
                "count": 0,
                "symbol": sym,
                "trade_date": None,
            }

        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.stock_forecast_terrain_intraday
            WHERE symbol = %s AND trade_date = %s
            ORDER BY asof_ts ASC
        """
        with conn.cursor() as cur:
            cur.execute(sql, (sym, resolved))
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()
    return {
        "rows": rows,
        "count": len(rows),
        "symbol": sym,
        "trade_date": resolved.isoformat() if resolved else None,
    }


@router.get("/gex/intraday")
def gex_intraday(
    symbol: str = Query(...),
    date_param: date | None = Query(None, alias="date"),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "symbol",
        "trade_date",
        "asof_ts",
        "spot",
        "total_net_gex",
        "zero_gamma",
        "major_call_wall",
        "major_put_wall",
        "levels_json",
        "computed_at",
    )
    sym = symbol.strip().upper()
    try:
        resolved = date_param
        if resolved is None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(trade_date) FROM features.option_metric_gex_intraday WHERE symbol = %s",
                    (sym,),
                )
                row = cur.fetchone()
            if row is not None:
                resolved = _as_date(
                    row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
                )
        if resolved is None:
            return {
                "rows": [],
                "count": 0,
                "symbol": sym,
                "trade_date": None,
            }

        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.option_metric_gex_intraday
            WHERE symbol = %s AND trade_date = %s
            ORDER BY asof_ts ASC
        """
        with conn.cursor() as cur:
            cur.execute(sql, (sym, resolved))
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()
    return {
        "rows": rows,
        "count": len(rows),
        "symbol": sym,
        "trade_date": resolved.isoformat() if resolved else None,
    }


# ---------------------------------------------------------------------------
# 4.1 Market Terrain
# ---------------------------------------------------------------------------


class TerrainComputeBody(BaseModel):
    symbol: str
    trade_date: date
    spot: float
    gex: dict[str, Any] | None = None
    momentum: dict[str, Any] | None = None
    iv: dict[str, Any] | None = None


@router.post("/forecast/terrain/compute")
def compute_terrain(body: TerrainComputeBody) -> dict[str, Any]:
    terrain = compute_market_terrain(
        body.symbol,
        body.trade_date,
        spot=body.spot,
        gex=body.gex,
        momentum=body.momentum,
        iv=body.iv,
    )
    return {"terrain": terrain.to_dict(), "advisory": "D10 BLOCKED — advisory only"}


@router.get("/forecast/terrain")
def get_terrain(
    symbol: str = Query(...),
    trade_date: date | None = Query(None),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "symbol",
        "trade_date",
        "pin_score",
        "trend_release",
        "vol_squeeze",
        "tail_risk",
        "expected_close",
        "gamma_zone_low",
        "gamma_zone_high",
        "regime",
        "spot",
        "inputs_json",
        "computed_at",
    )
    sym = symbol.strip().upper()
    try:
        clauses = ["symbol = %s"]
        params: list[Any] = [sym]
        resolved = trade_date
        if resolved is None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(trade_date) FROM features.stock_forecast_terrain_daily WHERE symbol = %s",
                    (sym,),
                )
                row = cur.fetchone()
            if row is not None:
                resolved = _as_date(
                    row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
                )
        if resolved is not None:
            clauses.append("trade_date = %s")
            params.append(resolved)
        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.stock_forecast_terrain_daily
            WHERE {' AND '.join(clauses)}
            LIMIT 1
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchone()
        if raw is None:
            raise HTTPException(status_code=404, detail="No market terrain for symbol")
        return {
            "terrain": _row_dict(raw, cols),
            "symbol": sym,
            "trade_date": resolved.isoformat() if resolved else None,
        }
    finally:
        conn.close()


@router.get("/forecast/terrain/history")
def get_terrain_history(
    symbol: str = Query(...),
    limit: int = Query(30, ge=1, le=200),
) -> dict[str, Any]:
    """Analyze C.3 / B.6 — recent terrain daily rows ordered by trade_date DESC."""
    conn = _connect_or_503()
    cols = (
        "symbol",
        "trade_date",
        "pin_score",
        "trend_release",
        "vol_squeeze",
        "tail_risk",
        "expected_close",
        "gamma_zone_low",
        "gamma_zone_high",
        "regime",
        "spot",
        "inputs_json",
        "computed_at",
    )
    sym = symbol.strip().upper()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {', '.join(cols)}
                FROM features.stock_forecast_terrain_daily
                WHERE symbol = %s
                ORDER BY trade_date DESC
                LIMIT %s
                """,
                (sym, limit),
            )
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
        return {"symbol": sym, "rows": rows, "count": len(rows)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4.2 AI Forecast
# ---------------------------------------------------------------------------


class ForecastComputeBody(BaseModel):
    symbol: str
    trade_date: date
    spot: float
    gex: dict[str, Any] | None = None
    momentum: dict[str, Any] | None = None
    iv: dict[str, Any] | None = None
    enrich: bool = True


@router.post("/forecast/sessions/compute")
def compute_forecast_session(body: ForecastComputeBody) -> dict[str, Any]:
    terrain = compute_market_terrain(
        body.symbol,
        body.trade_date,
        spot=body.spot,
        gex=body.gex,
        momentum=body.momentum,
        iv=body.iv,
    )
    session = build_forecast_session(
        terrain,
        llm=get_default_provider(),
        enrich=body.enrich,
    )
    return session.to_dict()


@router.get("/forecast/sessions")
def list_forecast_sessions(
    symbol: str | None = Query(None),
    trade_date: date | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "session_id",
        "symbol",
        "trade_date",
        "regime",
        "spot",
        "prob_rangy",
        "prob_bull",
        "prob_bear",
        "prob_squeeze",
        "expected_close",
        "structures_json",
        "narrative",
        "llm_provider",
        "advisory",
        "computed_at",
    )
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = %s")
            params.append(symbol.strip().upper())
        if trade_date is not None:
            clauses.append("trade_date = %s")
            params.append(trade_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.stock_forecast_session
            {where}
            ORDER BY trade_date DESC, computed_at DESC
            LIMIT %s
        """
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()
    return {"rows": rows, "count": len(rows)}


@router.get("/forecast/sessions/{session_id}")
def get_forecast_session(session_id: str) -> dict[str, Any]:
    conn = _connect_or_503()
    session_cols = (
        "session_id",
        "symbol",
        "trade_date",
        "regime",
        "spot",
        "prob_rangy",
        "prob_bull",
        "prob_bear",
        "prob_squeeze",
        "expected_close",
        "structures_json",
        "narrative",
        "llm_provider",
        "terrain_json",
        "advisory",
        "computed_at",
    )
    hourly_cols = (
        "session_id",
        "symbol",
        "trade_date",
        "hour_et",
        "path_call",
        "level_low",
        "level_high",
        "level_target",
        "confidence",
        "notes",
        "computed_at",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(session_cols)} FROM features.stock_forecast_session WHERE session_id = %s",
                (session_id,),
            )
            raw = cur.fetchone()
            if raw is None:
                raise HTTPException(status_code=404, detail="Forecast session not found")
            session = _row_dict(raw, session_cols)
            cur.execute(
                f"""
                SELECT {', '.join(hourly_cols)}
                FROM features.stock_forecast_hourly
                WHERE session_id = %s
                ORDER BY hour_et ASC
                """,
                (session_id,),
            )
            hourly_raw = cur.fetchall() or []
        hourly = [_row_dict(r, hourly_cols) for r in hourly_raw]
    finally:
        conn.close()
    return {"session": session, "hourly": hourly, "count_hourly": len(hourly)}


@router.get("/forecast/hourly")
def list_forecast_hourly(
    session_id: str = Query(...),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "session_id",
        "symbol",
        "trade_date",
        "hour_et",
        "path_call",
        "level_low",
        "level_high",
        "level_target",
        "confidence",
        "notes",
        "computed_at",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {', '.join(cols)}
                FROM features.stock_forecast_hourly
                WHERE session_id = %s
                ORDER BY hour_et ASC
                """,
                (session_id,),
            )
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()
    if not rows:
        raise HTTPException(status_code=404, detail="No hourly forecasts for session")
    return {"rows": rows, "count": len(rows), "session_id": session_id}


# ---------------------------------------------------------------------------
# 4.3 Event Radar
# ---------------------------------------------------------------------------


class EventRadarBody(BaseModel):
    payload: str = Field(..., min_length=1, description="Plain-text sample or bullet list")
    source: str = "sample"
    collected_at: date | None = None


@router.post("/event-radar/run")
def event_radar_run(body: EventRadarBody) -> dict[str, Any]:
    result = run_pipeline(
        body.payload,
        source=body.source,
        collected_at=body.collected_at,
    )
    return result.to_dict()


@router.get("/event-radar/events")
def list_event_radar(
    batch_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    include_dropped: bool = Query(False),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "event_id",
        "batch_id",
        "collected_at",
        "source",
        "subject",
        "event_summary",
        "affected_symbols",
        "direction",
        "certainty",
        "sentiment",
        "theme",
        "importance",
        "event_date",
        "dropped",
        "drop_reason",
        "raw_text",
        "computed_at",
    )
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if batch_id:
            clauses.append("batch_id = %s")
            params.append(batch_id)
        if not include_dropped:
            clauses.append("(dropped IS NULL OR dropped = false)")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.event_signal_radar_daily
            {where}
            ORDER BY collected_at DESC NULLS LAST, importance DESC NULLS LAST
            LIMIT %s
        """
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()
    return {"rows": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# 6.5 Event Radar — enhanced endpoints
# ---------------------------------------------------------------------------


@router.post("/events/ingest")
def event_ingest(body: EventRadarBody) -> dict[str, Any]:
    """Alias for event-radar/run — accepts raw text, returns PipelineResult."""
    result = run_pipeline(
        body.payload,
        source=body.source,
        collected_at=body.collected_at,
    )
    return result.to_dict()


@router.get("/events/batches")
def list_event_batches(
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    batch_id,
                    MIN(collected_at) AS collected_at,
                    COUNT(*) FILTER (WHERE dropped IS DISTINCT FROM true) AS kept_count,
                    COUNT(*) FILTER (WHERE dropped = true) AS dropped_count,
                    COUNT(*) AS total_count
                FROM features.event_signal_radar_daily
                WHERE batch_id IS NOT NULL
                GROUP BY batch_id
                ORDER BY MIN(collected_at) DESC NULLS LAST
                LIMIT %s
                """,
                (limit,),
            )
            raw = cur.fetchall() or []
        batch_cols = ("batch_id", "collected_at", "kept_count", "dropped_count", "total_count")
        rows = [_row_dict(r, batch_cols) for r in raw]
    finally:
        conn.close()
    return {"rows": rows, "count": len(rows)}


@router.get("/events/batch/{batch_id}")
def get_event_batch(batch_id: str) -> dict[str, Any]:
    conn = _connect_or_503()
    event_cols = (
        "event_id", "batch_id", "collected_at", "source", "subject",
        "event_summary", "affected_symbols", "direction", "certainty",
        "sentiment", "theme", "importance", "dropped", "drop_reason",
        "self_check_json", "computed_at",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {', '.join(event_cols)}
                FROM features.event_signal_radar_daily
                WHERE batch_id = %s
                ORDER BY importance DESC NULLS LAST
                """,
                (batch_id,),
            )
            raw = cur.fetchall() or []
        rows = [_row_dict(r, event_cols) for r in raw]
    finally:
        conn.close()
    if not rows:
        raise HTTPException(status_code=404, detail="Batch not found")
    return {"batch_id": batch_id, "rows": rows, "count": len(rows)}


@router.get("/events/themes")
def event_themes() -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    theme,
                    COUNT(*) AS count,
                    COUNT(*) FILTER (WHERE direction > 0) AS bull_count,
                    COUNT(*) FILTER (WHERE direction < 0) AS bear_count,
                    COUNT(*) FILTER (WHERE direction = 0) AS neutral_count,
                    ROUND(AVG(direction)::numeric, 2) AS direction_avg,
                    ROUND(AVG(sentiment)::numeric, 2) AS sentiment_avg
                FROM features.event_signal_radar_daily
                WHERE dropped IS DISTINCT FROM true
                  AND theme IS NOT NULL AND theme != ''
                GROUP BY theme
                ORDER BY count DESC
                """
            )
            raw = cur.fetchall() or []
        cols = (
            "theme",
            "count",
            "bull_count",
            "bear_count",
            "neutral_count",
            "direction_avg",
            "sentiment_avg",
        )
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()
    return {"rows": rows, "count": len(rows)}


@router.get("/event-radar/macro/gap")
def macro_gap(
    limit: int = Query(30, ge=1, le=200),
) -> dict[str, Any]:
    """Macro actual vs expected — rows with computed gap_pct."""
    conn = _connect_or_503()
    cols = (
        "macro_id",
        "event_date",
        "indicator",
        "actual_value",
        "expected_value",
        "prior_value",
        "unit",
        "gap_pct",
        "forward_flag",
        "source",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {', '.join(cols)}
                FROM features.macro_event_daily
                WHERE forward_flag IS NOT TRUE
                ORDER BY event_date DESC, computed_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()
    return {"rows": rows, "count": len(rows), "read_path": "features.macro_event_daily"}


@router.get("/event-radar/macro/forward")
def macro_forward(
    days: int = Query(7, ge=1, le=30),
) -> dict[str, Any]:
    """Forward macro calendar — upcoming releases."""
    conn = _connect_or_503()
    cols = (
        "macro_id",
        "event_date",
        "indicator",
        "expected_value",
        "prior_value",
        "unit",
        "forward_flag",
        "source",
        "notes",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {', '.join(cols)}
                FROM features.macro_event_daily
                WHERE forward_flag = true
                  AND event_date >= CURRENT_DATE
                  AND event_date <= CURRENT_DATE + %s * INTERVAL '1 day'
                ORDER BY event_date ASC
                LIMIT 100
                """,
                (days,),
            )
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()
    return {"rows": rows, "count": len(rows), "days": days}


@router.get("/events/calendar")
def event_calendar(
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "event_id", "batch_id", "collected_at", "source", "subject",
        "event_summary", "affected_symbols", "direction", "certainty",
        "sentiment", "theme", "importance", "event_date", "date_basis",
        "computed_at",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {', '.join(cols)}
                FROM features.event_signal_radar_daily
                WHERE dropped IS DISTINCT FROM true
                  AND time_code = 2
                ORDER BY event_date ASC NULLS LAST, importance DESC NULLS LAST
                LIMIT %s
                """,
                (limit,),
            )
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()
    return {"rows": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# 4.4 Backtest / Settlement
# ---------------------------------------------------------------------------


class SettlementBody(BaseModel):
    session_id: str
    symbol: str
    trade_date: date
    expected_close: float
    actual_close: float
    hourly: list[dict[str, Any]] = Field(default_factory=list)
    hourly_actuals: dict[int, float] | None = None


class AggregateBody(BaseModel):
    settlements: list[dict[str, Any]]
    symbol: str | None = None


@router.post("/backtest/settle")
def run_settlement(body: SettlementBody) -> dict[str, Any]:
    result = settle_forecast(
        session_id=body.session_id,
        symbol=body.symbol,
        trade_date=body.trade_date,
        expected_close=body.expected_close,
        hourly=body.hourly,
        actual_close=body.actual_close,
        hourly_actuals=body.hourly_actuals,
    )
    return result.to_dict()


@router.post("/backtest/aggregate")
def run_aggregate(body: AggregateBody) -> dict[str, Any]:
    from bifrost_research.engines.backtest.settlement import ForecastSettlement, HourlyActual

    parsed = []
    for s in body.settlements:
        hourly = [
            HourlyActual(
                hour_et=int(h.get("hour_et") or 0),
                path_call=str(h.get("path_call") or ""),
                level_low=float(h.get("level_low") or 0),
                level_high=float(h.get("level_high") or 0),
                level_target=float(h.get("level_target") or 0),
                actual_price=h.get("actual_price"),
                hit=bool(h.get("hit")),
            )
            for h in (s.get("hourly") or [])
        ]
        parsed.append(
            ForecastSettlement(
                settlement_id=str(s.get("settlement_id") or "tmp"),
                session_id=str(s.get("session_id") or ""),
                symbol=str(s.get("symbol") or body.symbol or ""),
                trade_date=_as_date(s.get("trade_date")) or date.today(),
                expected_close=float(s.get("expected_close") or 0),
                actual_close=float(s.get("actual_close") or 0),
                close_miss=float(s.get("close_miss") or 0),
                close_miss_pct=float(s.get("close_miss_pct") or 0),
                path_hit=bool(s.get("path_hit")),
                path_hit_count=int(s.get("path_hit_count") or 0),
                path_total=int(s.get("path_total") or 0),
                hourly=hourly,
                notes=str(s.get("notes") or ""),
            )
        )
    summary = aggregate_accuracy(parsed, symbol=body.symbol)
    return summary.to_dict()


@router.get("/forecast/settlement")
def get_forecast_settlement(
    symbol: str | None = Query(None),
    trade_date: date | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Settlement results for forecast sessions — by date and/or symbol."""
    conn = _connect_or_503()
    cols = (
        "settlement_id",
        "session_id",
        "symbol",
        "trade_date",
        "expected_close",
        "actual_close",
        "close_miss",
        "close_miss_pct",
        "path_hit",
        "path_hit_count",
        "path_total",
        "hourly_json",
        "notes",
        "stats_json",
        "computed_at",
    )
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = %s")
            params.append(symbol.strip().upper())
        if trade_date is not None:
            clauses.append("trade_date = %s")
            params.append(trade_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.stock_backtest_settlement
            {where}
            ORDER BY trade_date DESC, computed_at DESC
            LIMIT %s
        """
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows = [_enrich_settlement_row(_row_dict(r, cols)) for r in raw]
    finally:
        conn.close()
    return {"rows": rows, "count": len(rows)}


@router.get("/forecast/hit-rate")
def forecast_hit_rate(
    symbol: str = Query(..., min_length=1, max_length=32),
    lookback_days: int = Query(30, ge=1, le=365),
) -> dict[str, Any]:
    """Analyze C.1 — hit-rate summary from existing ``stock_backtest_settlement``.

    Does not create a duplicate realized table; path_hit / close_miss already live
    on ``features.stock_backtest_settlement``.
    """
    conn = _connect_or_503()
    sym = symbol.strip().upper()
    summary_cols = (
        "settlement_id",
        "session_id",
        "symbol",
        "trade_date",
        "expected_close",
        "actual_close",
        "close_miss_pct",
        "path_hit",
        "stats_json",
        "computed_at",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*)::bigint AS session_count,
                    ROUND(
                        AVG(CASE WHEN path_hit THEN 1.0 ELSE 0.0 END)::numeric, 4
                    ) AS path_hit_rate,
                    ROUND(AVG(ABS(close_miss_pct))::numeric, 6) AS avg_close_miss_pct,
                    ROUND(
                        AVG(
                            CASE
                                WHEN stats_json ? 'direction_hit'
                                THEN CASE
                                    WHEN (stats_json->>'direction_hit')::boolean THEN 1.0
                                    ELSE 0.0
                                END
                                ELSE NULL
                            END
                        )::numeric,
                        4
                    ) AS direction_hit_rate
                FROM features.stock_backtest_settlement
                WHERE symbol = %s
                  AND trade_date >= CURRENT_DATE - (%s::integer)
                """,
                (sym, lookback_days),
            )
            agg = cur.fetchone()
            cur.execute(
                f"""
                SELECT {', '.join(summary_cols)}
                FROM features.stock_backtest_settlement
                WHERE symbol = %s
                  AND trade_date >= CURRENT_DATE - (%s::integer)
                ORDER BY trade_date DESC, computed_at DESC
                LIMIT 50
                """,
                (sym, lookback_days),
            )
            raw = cur.fetchall() or []
        if agg is None or isinstance(agg, Mapping):
            session_count = int((agg or {}).get("session_count") or 0) if isinstance(agg, Mapping) else 0
            path_hit_rate = float((agg or {}).get("path_hit_rate") or 0) if isinstance(agg, Mapping) else 0.0
            avg_close_miss_pct = (
                float((agg or {}).get("avg_close_miss_pct") or 0) if isinstance(agg, Mapping) else 0.0
            )
            direction_hit_rate = (
                float((agg or {}).get("direction_hit_rate") or 0)
                if isinstance(agg, Mapping) and (agg or {}).get("direction_hit_rate") is not None
                else None
            )
        else:
            session_count = int(agg[0] or 0)
            path_hit_rate = float(agg[1] or 0)
            avg_close_miss_pct = float(agg[2] or 0)
            direction_hit_rate = float(agg[3]) if agg[3] is not None else None
        rows = [_row_dict(r, summary_cols) for r in raw]
        # Flatten direction_hit onto row summaries when present
        for item in rows:
            stats = item.get("stats_json")
            if isinstance(stats, dict) and "direction_hit" in stats:
                item["direction_hit"] = stats.get("direction_hit")
        return {
            "symbol": sym,
            "lookback_days": lookback_days,
            "session_count": session_count,
            "path_hit_rate": path_hit_rate,
            "avg_close_miss_pct": avg_close_miss_pct,
            "direction_hit_rate": direction_hit_rate,
            "rows": rows,
        }
    finally:
        conn.close()


@router.post("/forecast/settle")
def trigger_settlement(body: SettlementBody) -> dict[str, Any]:
    """Manual settlement trigger for a single session."""
    result = settle_forecast(
        session_id=body.session_id,
        symbol=body.symbol,
        trade_date=body.trade_date,
        expected_close=body.expected_close,
        hourly=body.hourly,
        actual_close=body.actual_close,
        hourly_actuals=body.hourly_actuals,
    )
    return result.to_dict()


@router.get("/forecast/backtest")
def forecast_backtest(
    symbol: str = Query(...),
    start: date | None = Query(None),
    end: date | None = Query(None),
) -> dict[str, Any]:
    """Aggregated backtest statistics over a date range."""
    conn = _connect_or_503()
    try:
        clauses = ["symbol = %s"]
        params: list[Any] = [symbol.strip().upper()]
        if start:
            clauses.append("trade_date >= %s")
            params.append(start)
        if end:
            clauses.append("trade_date <= %s")
            params.append(end)
        where = f"WHERE {' AND '.join(clauses)}"
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS sessions_settled,
                    ROUND(AVG(CASE WHEN path_hit THEN 1.0 ELSE 0.0 END)::numeric, 4) AS path_hit_rate,
                    ROUND(AVG(ABS(close_miss_pct))::numeric, 6) AS avg_close_miss_pct,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ABS(close_miss_pct)) AS median_close_miss_pct
                FROM features.stock_backtest_settlement
                {where}
                """,
                tuple(params),
            )
            row = cur.fetchone()
        if row is None:
            return {"sessions_settled": 0, "path_hit_rate": 0, "avg_close_miss_pct": 0}
        if isinstance(row, Mapping):
            result = dict(row)
        else:
            result = {
                "sessions_settled": row[0] or 0,
                "path_hit_rate": float(row[1] or 0),
                "avg_close_miss_pct": float(row[2] or 0),
                "median_close_miss_pct": float(row[3] or 0),
            }
    finally:
        conn.close()
    return {
        "symbol": symbol.strip().upper(),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        **result,
    }


@router.get("/backtest/settlement")
def get_settlement_summary(
    symbol: str | None = Query(None),
    session_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "settlement_id",
        "session_id",
        "symbol",
        "trade_date",
        "expected_close",
        "actual_close",
        "close_miss",
        "close_miss_pct",
        "path_hit",
        "path_hit_count",
        "path_total",
        "notes",
        "stats_json",
        "computed_at",
    )
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = %s")
            params.append(symbol.strip().upper())
        if session_id:
            clauses.append("session_id = %s")
            params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.stock_backtest_settlement
            {where}
            ORDER BY trade_date DESC
            LIMIT %s
        """
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows = [_enrich_settlement_row(_row_dict(r, cols)) for r in raw]
    finally:
        conn.close()
    return {"rows": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Wave R8 — Daily Brief synth + regime stats
# ---------------------------------------------------------------------------


@router.get("/daily-brief/synth")
def daily_brief_synth(
    symbol: str = Query(...),
    date_param: date | None = Query(None, alias="date"),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        return synthesize_daily_brief(conn, symbol, date_param)
    finally:
        conn.close()


@router.get("/backtest/regime-stats")
def backtest_regime_stats(
    symbol: str = Query(...),
    lookback_days: int = Query(60, ge=7, le=365),
    current_regime: str | None = Query(None),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        return compute_regime_stats(
            conn,
            symbol,
            lookback_days=lookback_days,
            current_regime=current_regime,
        )
    finally:
        conn.close()
