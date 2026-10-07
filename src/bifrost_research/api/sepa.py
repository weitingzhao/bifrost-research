"""SEPA analytics routes — ``/analytics/sepa/*`` (dbt marts on Golden Source)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.api import sepa_reader, tier_reader

router = APIRouter(prefix="/analytics/sepa", tags=["sepa"])


@router.get("/criteria-stats")
def criteria_stats() -> dict[str, Any]:
    """Per-domain pass/fail stats, plus names per conditions-passed count (0.157.0).

    ``fundamental_distribution`` (8..0) / ``technical_distribution`` (11..0) are
    ``[{conditions_passed, symbol_count}]`` on each mart's latest eval_date, given
    as ``fundamental_eval_date`` / ``technical_eval_date``.
    """
    try:
        raw = sepa_reader.fetch_criteria_stats()
        dist = sepa_reader.fetch_pass_count_distributions()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Analytics DB error: {exc}") from exc
    return {"ok": True, **raw, **dist}


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


# ── Momentum · structure · sentiment tiers (0.157.0, TD-49) ──────────────────


def _tier_or_400(tier: str) -> str:
    if tier not in tier_reader.TIER_COLUMNS:
        raise HTTPException(status_code=400, detail=f"tier must be one of: {list(tier_reader.TIER_COLUMNS.keys())}")
    return tier


@router.get("/tier-stats")
def tier_stats(tier: str = Query("momentum", description="momentum | structure | sentiment")) -> dict[str, Any]:
    """Per-signal pass counts and the signals-passed histogram (0..N) on the tier mart's latest eval_date."""
    _tier_or_400(tier)
    try:
        return tier_reader.fetch_tier_stats(tier)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Analytics DB error: {exc}") from exc


@router.get("/tier-filter")
def tier_filter(
    tier: str = Query(..., description="momentum | structure | sentiment"),
    include: str = Query("", description="Comma-separated signal ids (the mart's boolean columns)"),
    min_score: int = Query(0, description="At least this many signals passed (clamped to 0..N)"),
    match: str = Query("all", description="all | any of the picked signals"),
    limit: int = Query(500, description="Names returned (clamped to 1..5000)"),
) -> dict[str, Any]:
    """Names passing the picked signals and at least ``min_score`` of them, latest eval_date.

    ``count`` is the whole match (``count(*) OVER ()``), not the page; ``truncated``
    says the list stops short of it. An unknown signal id is 400.
    """
    _tier_or_400(tier)
    raw_ids = [s.strip() for s in (include or "").split(",") if s.strip()]
    valid = set(tier_reader.TIER_COLUMNS[tier])
    unknown = [c for c in raw_ids if c not in valid]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown {tier} signal ids: {', '.join(unknown)}")
    eff_limit = max(1, min(int(limit), 5000))
    eff_min = max(0, min(int(min_score or 0), tier_reader.TIER_MAX_SCORE[tier]))
    eff_match = "any" if match == "any" else "all"
    if not raw_ids and eff_min == 0:
        return {"ok": True, "tier": tier, "include": [], "count": 0, "symbols": [], "limit": eff_limit}
    try:
        return tier_reader.fetch_tier_filter(tier, raw_ids, eff_min, eff_match, eff_limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Analytics DB error: {exc}") from exc
