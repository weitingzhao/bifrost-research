"""SEPA analytics routes — ``/analytics/sepa/*`` (dbt marts on Golden Source)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.api import sepa_reader

router = APIRouter(prefix="/analytics/sepa", tags=["sepa"])


@router.get("/criteria-stats")
def criteria_stats() -> dict[str, Any]:
    try:
        raw = sepa_reader.fetch_criteria_stats()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Analytics DB error: {exc}") from exc
    return {"ok": True, **raw}


@router.get("/fundamental-eval/{symbol}")
def fundamental_eval(symbol: str) -> dict[str, Any]:
    try:
        row = sepa_reader.fetch_fundamental_eval_single(symbol)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Analytics DB error: {exc}") from exc
    if row is None:
        raise HTTPException(status_code=404, detail=f"No fundamental eval for {symbol.upper()}")
    return {"ok": True, "symbol": symbol.upper(), "row": row}


@router.get("/technical-eval/{symbol}")
def technical_eval(symbol: str) -> dict[str, Any]:
    try:
        row = sepa_reader.fetch_technical_eval_single(symbol)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Analytics DB error: {exc}") from exc
    if row is None:
        raise HTTPException(status_code=404, detail=f"No technical eval for {symbol.upper()}")
    return {"ok": True, "symbol": symbol.upper(), "row": row}


@router.get("/fundamental-filter")
def fundamental_filter(
    conditions: str = Query("", description="Comma-separated fundamental condition column ids"),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    cond_ids = [s.strip() for s in (conditions or "").split(",") if s.strip()]
    if not cond_ids:
        return {"ok": True, "conditions": [], "count": 0, "symbols": [], "limit": limit}
    valid = [c for c in cond_ids if c in sepa_reader.FUND_CONDITION_COLUMNS]
    if not valid:
        raise HTTPException(status_code=400, detail="no valid fundamental condition IDs")
    try:
        rows = sepa_reader.fetch_fundamental_filter(valid, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    symbols = [
        {
            "symbol": r["symbol"],
            "pass_count": int(r.get("pass_count") or 0),
            "passed_conditions": valid,
        }
        for r in rows
    ]
    return {"ok": True, "conditions": valid, "count": len(symbols), "symbols": symbols, "limit": limit}


@router.get("/technical-filter")
def technical_filter(
    conditions: str = Query("", description="Comma-separated technical condition column ids"),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    cond_ids = [s.strip() for s in (conditions or "").split(",") if s.strip()]
    if not cond_ids:
        return {"ok": True, "conditions": [], "count": 0, "symbols": [], "limit": limit}
    valid = [c for c in cond_ids if c in sepa_reader.TECH_CONDITION_COLUMNS]
    if not valid:
        raise HTTPException(status_code=400, detail="no valid technical condition IDs")
    try:
        rows = sepa_reader.fetch_technical_filter(valid, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    symbols = [
        {
            "symbol": r["symbol"],
            "pass_count": int(r.get("pass_count") or 0),
            "passed_conditions": valid,
        }
        for r in rows
    ]
    return {"ok": True, "conditions": valid, "count": len(symbols), "symbols": symbols, "limit": limit}


@router.get("/fundamental-distribution")
def fundamental_distribution(
    conditions_passed: int = Query(..., ge=0, le=8),
) -> dict[str, Any]:
    try:
        symbols = sepa_reader.fetch_fundamental_distribution_symbols(conditions_passed)
        as_of = sepa_reader.peek_latest_eval_date(sepa_reader._FUND_EVAL_TABLE)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "ok": True,
        "conditions_passed": conditions_passed,
        "count": len(symbols),
        "symbols": symbols,
        "as_of": as_of,
    }


@router.get("/technical-distribution")
def technical_distribution(
    conditions_passed: int = Query(..., ge=0, le=11),
) -> dict[str, Any]:
    try:
        symbols = sepa_reader.fetch_technical_distribution_symbols(conditions_passed)
        as_of = sepa_reader.peek_latest_eval_date(sepa_reader._TECH_EVAL_TABLE)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "ok": True,
        "conditions_passed": conditions_passed,
        "count": len(symbols),
        "symbols": symbols,
        "as_of": as_of,
    }


@router.get("/screener-wide")
def screener_wide(
    symbols: str = Query("", description="Comma-separated symbols (optional)"),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    sym_list = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()] or None
    try:
        rows = sepa_reader.fetch_screener_wide(symbols=sym_list, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "count": len(rows), "rows": rows, "limit": limit}


@router.get("/screening-ranked")
def screening_ranked(
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    try:
        rows = sepa_reader.fetch_screening_ranked(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "count": len(rows), "rows": rows, "limit": limit}
