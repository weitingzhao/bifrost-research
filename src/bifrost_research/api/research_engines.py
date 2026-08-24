"""Research engine read APIs — ``/research/*`` (Wave 3)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect

router = APIRouter(prefix="/research", tags=["research-engines"])


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
    for key in ("trade_date", "expiry"):
        if key in out and out[key] is not None:
            d = _as_date(out[key])
            out[key] = d.isoformat() if d else out[key]
    if "computed_at" in out and isinstance(out["computed_at"], datetime):
        out["computed_at"] = out["computed_at"].isoformat()
    return out


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


# ---------------------------------------------------------------------------
# Momentum Radar
# ---------------------------------------------------------------------------


@router.get("/momentum/radar")
def momentum_radar(
    symbol: str | None = Query(None, description="Underlying symbol filter"),
    trade_date: date | None = Query(None, description="Trade date (default: latest)"),
    grade: str | None = Query(None, description="Filter grade A+/A/B/C/D"),
    path: str | None = Query(None, description="Filter path EXT/PB/FAIL/HALT"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "symbol",
        "trade_date",
        "score",
        "grade",
        "path",
        "z_sdt",
        "z_v",
        "accept_vwap",
        "z_ofi",
        "h_52w",
        "o_plus",
        "a_factor",
        "r_sec",
        "crash",
        "factors_json",
        "computed_at",
    )
    try:
        clauses: list[str] = []
        params: list[Any] = []
        sym = str(symbol).strip().upper() if symbol else None
        if sym:
            clauses.append("symbol = %s")
            params.append(sym)
        resolved = trade_date
        if resolved is None and sym:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(trade_date) FROM features.stock_signal_momentum_daily WHERE symbol = %s",
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
        if grade:
            clauses.append("grade = %s")
            params.append(grade.strip().upper())
        if path:
            clauses.append("path = %s")
            params.append(path.strip().upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.stock_signal_momentum_daily
            {where}
            ORDER BY score DESC NULLS LAST, symbol ASC
            LIMIT %s
        """
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()

    if not rows and sym:
        raise HTTPException(status_code=404, detail="No momentum radar rows for symbol")
    return {
        "rows": rows,
        "count": len(rows),
        "symbol": sym,
        "trade_date": resolved.isoformat() if resolved else None,
    }


# ---------------------------------------------------------------------------
# GEX
# ---------------------------------------------------------------------------


@router.get("/gex/levels")
def gex_levels(
    symbol: str = Query(..., description="Underlying symbol"),
    trade_date: date | None = Query(None),
    expiry: date | None = Query(None),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "symbol",
        "trade_date",
        "expiry",
        "spot",
        "total_net_gex",
        "zero_gamma",
        "major_call_wall",
        "major_put_wall",
        "call_wall_gex",
        "put_wall_gex",
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
                    "SELECT MAX(trade_date) FROM features.option_metric_gex_levels_daily WHERE symbol = %s",
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
        if expiry is not None:
            clauses.append("expiry = %s")
            params.append(expiry)
        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.option_metric_gex_levels_daily
            WHERE {' AND '.join(clauses)}
            ORDER BY expiry ASC
            LIMIT 200
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail="No GEX levels for symbol")
    return {
        "rows": rows,
        "count": len(rows),
        "symbol": sym,
        "trade_date": resolved.isoformat() if resolved else None,
    }


@router.get("/gex/distribution")
def gex_distribution(
    symbol: str = Query(..., description="Underlying symbol"),
    trade_date: date | None = Query(None),
    expiry: date | None = Query(None),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "symbol",
        "trade_date",
        "expiry",
        "strike",
        "call_oi",
        "put_oi",
        "call_volume",
        "put_volume",
        "call_gex",
        "put_gex",
        "net_gex",
        "gex_source",
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
                    "SELECT MAX(trade_date) FROM features.option_metric_gex_daily WHERE symbol = %s",
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
        if expiry is not None:
            clauses.append("expiry = %s")
            params.append(expiry)
        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.option_metric_gex_daily
            WHERE {' AND '.join(clauses)}
            ORDER BY expiry ASC, strike ASC
            LIMIT 5000
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail="No GEX distribution for symbol")
    return {
        "rows": rows,
        "count": len(rows),
        "symbol": sym,
        "trade_date": resolved.isoformat() if resolved else None,
        "expiry": expiry.isoformat() if expiry else None,
    }


# ---------------------------------------------------------------------------
# IV Surface / Smile
# ---------------------------------------------------------------------------


@router.get("/volatility/smile")
def volatility_smile(
    symbol: str = Query(..., description="Underlying symbol"),
    trade_date: date | None = Query(None),
    expiry: date | None = Query(None),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "symbol",
        "trade_date",
        "expiry",
        "spot",
        "fit_model",
        "smile_params",
        "rmse",
        "n_points",
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
                    "SELECT MAX(trade_date) FROM features.option_surface_iv_daily WHERE symbol = %s",
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
        if expiry is not None:
            clauses.append("expiry = %s")
            params.append(expiry)
        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.option_surface_iv_daily
            WHERE {' AND '.join(clauses)}
            ORDER BY expiry ASC
            LIMIT 100
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail="No IV smile rows for symbol")
    return {
        "rows": rows,
        "count": len(rows),
        "symbol": sym,
        "trade_date": resolved.isoformat() if resolved else None,
    }


@router.get("/volatility/surface")
def volatility_surface(
    symbol: str = Query(..., description="Underlying symbol"),
    trade_date: date | None = Query(None),
    expiry: date | None = Query(None),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "symbol",
        "trade_date",
        "expiry",
        "spot",
        "fit_model",
        "surface_points",
        "vol_cone",
        "rmse",
        "n_points",
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
                    "SELECT MAX(trade_date) FROM features.option_surface_iv_daily WHERE symbol = %s",
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
        if expiry is not None:
            clauses.append("expiry = %s")
            params.append(expiry)
        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.option_surface_iv_daily
            WHERE {' AND '.join(clauses)}
            ORDER BY expiry ASC
            LIMIT 100
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail="No IV surface rows for symbol")

    # Flatten surface points for convenience
    points: list[dict[str, Any]] = []
    vol_cone = None
    for r in rows:
        pts = r.get("surface_points") or []
        if isinstance(pts, list):
            points.extend(pts)
        if vol_cone is None and r.get("vol_cone"):
            vol_cone = r["vol_cone"]

    return {
        "rows": rows,
        "surface_points": points,
        "vol_cone": vol_cone,
        "count": len(rows),
        "symbol": sym,
        "trade_date": resolved.isoformat() if resolved else None,
    }


# ---------------------------------------------------------------------------
# Order Flow
# ---------------------------------------------------------------------------


@router.get("/flow/sentiment")
def flow_sentiment(
    symbol: str | None = Query(None),
    trade_date: date | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "symbol",
        "trade_date",
        "call_notional",
        "put_notional",
        "sentiment_score",
        "call_volume",
        "put_volume",
        "call_oi",
        "put_oi",
        "pcr_volume",
        "pcr_oi",
        "expiry_concentration",
        "strike_concentration",
        "data_source",
        "notes",
        "computed_at",
    )
    try:
        clauses: list[str] = []
        params: list[Any] = []
        sym = str(symbol).strip().upper() if symbol else None
        if sym:
            clauses.append("symbol = %s")
            params.append(sym)
        resolved = trade_date
        if resolved is None and sym:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(trade_date) FROM features.option_flow_sentiment_daily WHERE symbol = %s",
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
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.option_flow_sentiment_daily
            {where}
            ORDER BY trade_date DESC, sentiment_score DESC NULLS LAST
            LIMIT %s
        """
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()

    if not rows and sym:
        raise HTTPException(status_code=404, detail="No order sentiment rows for symbol")
    return {
        "rows": rows,
        "count": len(rows),
        "symbol": sym,
        "trade_date": resolved.isoformat() if resolved else None,
        "data_source_note": (
            "Prefers market.option_trades (data_source=option_trades_tape) when "
            "tape rows exist; otherwise option_snapshot/OI proxy"
        ),
    }


@router.get("/flow/multi-leg")
def flow_multi_leg(
    symbol: str = Query(...),
    trade_date: date | None = Query(None),
) -> dict[str, Any]:
    conn = _connect_or_503()
    cols = (
        "symbol",
        "trade_date",
        "cluster_id",
        "strategy_guess",
        "legs",
        "total_notional",
        "confidence",
        "data_source",
        "notes",
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
                    "SELECT MAX(trade_date) FROM features.option_flow_multi_leg_daily WHERE symbol = %s",
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
            FROM features.option_flow_multi_leg_daily
            WHERE {' AND '.join(clauses)}
            ORDER BY total_notional DESC NULLS LAST
            LIMIT 200
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows = [_row_dict(r, cols) for r in raw]
    finally:
        conn.close()

    return {
        "rows": rows,
        "count": len(rows),
        "symbol": sym,
        "trade_date": resolved.isoformat() if resolved else None,
        "scaffolding": True,
        "data_source_note": (
            "Multi-leg scaffolding; prefer option_trades_tape when tape is present"
        ),
    }


# ---------------------------------------------------------------------------
# SEPA fusion (Wave B) — dashboard read = dw_stock; model read = features
# ---------------------------------------------------------------------------

_SEPA_MODEL_COLS = (
    "symbol",
    "trade_date",
    "fundamental_score",
    "trend_template_score",
    "momentum_score",
    "structure_score",
    "sepa_score",
    "grade",
    "stage",
    "path",
    "trend_template_pass",
    "fundamental_pass",
    "latest_close",
    "sma_50",
    "sma_150",
    "sma_200",
    "high_52w",
    "low_52w",
    "iv_percentile",
    "pcr_oi",
    "fund_pass_count",
    "tech_pass_count",
    "factors_json",
    "asof_ts",
    "computed_at",
)

_SEPA_DASHBOARD_COLS = (
    "symbol",
    "trade_date",
    "overall_rank",
    "decile",
    "percentile",
    "composite_score",
    "fund_pass_count",
    "tech_pass_count",
    "combined_pass_count",
    "momentum_score",
    "structure_score",
    "sentiment_score",
    "latest_close",
    "sma_50",
    "sma_150",
    "sma_200",
    "company_name",
    "primary_exchange",
)


@router.get("/sepa/daily")
def sepa_daily(
    symbol: str | None = Query(None, description="Filter by underlying symbol"),
    trade_date: date | None = Query(None, description="Eval date (default: latest)"),
    min_score: float | None = Query(None, ge=0, le=100, description="Minimum composite (0-100 scale)"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """SEPA dashboard read — ``dw_stock.mart_sepa_screener_wide`` (canonical human read)."""
    conn = _connect_or_503()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        sym = str(symbol).strip().upper() if symbol else None
        if sym:
            clauses.append("symbol = %s")
            params.append(sym)
        resolved = trade_date
        if resolved is None:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(eval_date) FROM dw_stock.mart_sepa_screener_wide")
                row = cur.fetchone()
            if row is not None:
                resolved = _as_date(
                    row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
                )
        if resolved is not None:
            clauses.append("eval_date = %s")
            params.append(resolved)
        if min_score is not None:
            clauses.append("composite_score * 100 >= %s")
            params.append(float(min_score))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT
                symbol,
                eval_date AS trade_date,
                overall_rank,
                decile,
                percentile,
                composite_score,
                fund_pass_count,
                tech_pass_count,
                combined_pass_count,
                momentum_score,
                structure_score,
                sentiment_score,
                latest_close,
                sma_50,
                sma_150,
                sma_200,
                company_name,
                primary_exchange
            FROM dw_stock.mart_sepa_screener_wide
            {where}
            ORDER BY composite_score DESC NULLS LAST, overall_rank ASC NULLS LAST, symbol ASC
            LIMIT %s
        """
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows = [_row_dict(r, _SEPA_DASHBOARD_COLS) for r in raw]
    finally:
        conn.close()

    return {
        "rows": rows,
        "count": len(rows),
        "symbol": sym,
        "trade_date": resolved.isoformat() if resolved else None,
        "read_path": "dw_stock.mart_sepa_screener_wide",
        "filters": {"min_score": min_score},
    }


@router.get("/sepa/candidates")
def sepa_candidates(
    trade_date: date | None = Query(None),
    top: int = Query(30, ge=1, le=100),
) -> dict[str, Any]:
    """SEPA dashboard short-list — top-N by ``overall_rank`` from screener wide mart."""
    conn = _connect_or_503()
    try:
        resolved = trade_date
        if resolved is None:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(eval_date) FROM dw_stock.mart_sepa_screener_wide")
                row = cur.fetchone()
            if row is not None:
                resolved = _as_date(
                    row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
                )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    symbol,
                    eval_date AS trade_date,
                    overall_rank,
                    decile,
                    percentile,
                    composite_score,
                    fund_pass_count,
                    tech_pass_count,
                    combined_pass_count,
                    momentum_score,
                    structure_score,
                    sentiment_score,
                    latest_close,
                    sma_50,
                    sma_150,
                    sma_200,
                    company_name,
                    primary_exchange
                FROM dw_stock.mart_sepa_screener_wide
                WHERE eval_date = %s
                ORDER BY overall_rank ASC NULLS LAST, composite_score DESC NULLS LAST
                LIMIT %s
                """,
                (resolved, top),
            )
            raw = cur.fetchall() or []
        rows = [_row_dict(r, _SEPA_DASHBOARD_COLS) for r in raw]
    finally:
        conn.close()

    return {
        "trade_date": resolved.isoformat() if resolved else None,
        "candidates": rows,
        "count": len(rows),
        "read_path": "dw_stock.mart_sepa_screener_wide",
    }


@router.get("/sepa/model/daily")
def sepa_model_daily(
    symbol: str | None = Query(None, description="Filter by underlying symbol"),
    trade_date: date | None = Query(None, description="Trade date (default: latest)"),
    stage: str | None = Query(None, description="Filter STAGE_1/2A/2B/2C/3/4"),
    path_filter: str | None = Query(None, alias="path", description="Filter SETUP/PIVOT/EXTENDED/WATCH/AVOID"),
    grade: str | None = Query(None, description="Filter grade A+/A/B/C/D"),
    min_score: float | None = Query(None, ge=0, le=100, description="Minimum SEPA composite"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """Model read — ``features.stock_signal_sepa_daily`` (projection from dbt mart)."""
    conn = _connect_or_503()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        sym = str(symbol).strip().upper() if symbol else None
        if sym:
            clauses.append("symbol = %s")
            params.append(sym)
        resolved = trade_date
        if resolved is None:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(trade_date) FROM features.stock_signal_sepa_daily")
                row = cur.fetchone()
            if row is not None:
                resolved = _as_date(
                    row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
                )
        if resolved is not None:
            clauses.append("trade_date = %s")
            params.append(resolved)
        if stage:
            clauses.append("stage = %s")
            params.append(stage.strip().upper())
        if path_filter:
            clauses.append("path = %s")
            params.append(path_filter.strip().upper())
        if grade:
            clauses.append("grade = %s")
            params.append(grade.strip().upper())
        if min_score is not None:
            clauses.append("sepa_score >= %s")
            params.append(float(min_score))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT {', '.join(_SEPA_MODEL_COLS)}
            FROM features.stock_signal_sepa_daily
            {where}
            ORDER BY sepa_score DESC NULLS LAST, symbol ASC
            LIMIT %s
        """
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows = [_row_dict(r, _SEPA_MODEL_COLS) for r in raw]
    finally:
        conn.close()

    return {
        "rows": rows,
        "count": len(rows),
        "symbol": sym,
        "trade_date": resolved.isoformat() if resolved else None,
        "read_path": "features.stock_signal_sepa_daily",
        "filters": {
            "stage": stage,
            "path": path_filter,
            "grade": grade,
            "min_score": min_score,
        },
    }


@router.get("/sepa/model/candidates")
def sepa_model_candidates(
    trade_date: date | None = Query(None),
    top: int = Query(30, ge=1, le=100),
) -> dict[str, Any]:
    """Model short-list — top SETUP/PIVOT from Feature Store projection."""
    conn = _connect_or_503()
    try:
        resolved = trade_date
        if resolved is None:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(trade_date) FROM features.stock_signal_sepa_daily")
                row = cur.fetchone()
            if row is not None:
                resolved = _as_date(
                    row[0] if not isinstance(row, Mapping) else next(iter(row.values()), None)
                )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {', '.join(_SEPA_MODEL_COLS)}
                FROM features.stock_signal_sepa_daily
                WHERE trade_date = %s
                  AND path IN ('SETUP', 'PIVOT')
                ORDER BY sepa_score DESC NULLS LAST
                LIMIT %s
                """,
                (resolved, top),
            )
            raw = cur.fetchall() or []
        rows = [_row_dict(r, _SEPA_MODEL_COLS) for r in raw]
    finally:
        conn.close()

    return {
        "trade_date": resolved.isoformat() if resolved else None,
        "candidates": rows,
        "count": len(rows),
        "read_path": "features.stock_signal_sepa_daily",
    }
