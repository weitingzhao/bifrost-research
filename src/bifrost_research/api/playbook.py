"""Playbook REST API — RS-KB3 + Analyze E.3 hit-rate."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.api.similar_regime import _fwd_return
from bifrost_research.auth.deps import require_owner
from bifrost_research.db.conn import connect
from bifrost_research.repositories import playbook as playbook_repo

router = APIRouter(prefix="/research/playbook", tags=["research-playbook"])

_DOMINANT_PREFIX = "dominant:"
_SCENARIO_KEYS = frozenset({"bull", "bear", "rangy", "squeeze"})


class RuleCreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    category: str = Field(default="general", max_length=64)
    body_md: str = Field(min_length=1)
    trigger_ctx: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    source_session_id: str | None = None


class RulePatchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=64)
    body_md: str | None = None
    trigger_ctx: dict[str, Any] | None = None
    tags: list[str] | None = None
    active: bool | None = None


class NoteCreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    note_md: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    source_session_id: str | None = None


class CaseCreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    lessons_md: str = Field(min_length=1)
    trade_ref: dict[str, Any] = Field(default_factory=dict)
    outcome: str | None = None
    tags: list[str] = Field(default_factory=list)
    related_rule_ids: list[str] = Field(default_factory=list)


class CaseFromBridgeBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bridge_event_id: str = Field(min_length=1)
    external_reply_md: str = Field(min_length=1)
    outcome: str | None = None
    tags: list[str] = Field(default_factory=list)


@router.get("/rules")
def list_rules(
    owner_id: str = Depends(require_owner),
    category: str | None = None,
    symbol: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    conn = connect()
    try:
        rows = playbook_repo.list_rules(
            conn,
            owner_id=owner_id,
            category=category,
            symbol=symbol,
            limit=min(limit, 100),
            active_only=False,
        )
        return {"ok": True, "data": {"rows": rows, "count": len(rows)}}
    finally:
        conn.close()


@router.post("/rules")
def create_rule(body: RuleCreateBody, owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    conn = connect()
    try:
        row = playbook_repo.create_rule(
            conn,
            owner_id=owner_id,
            title=body.title,
            category=body.category,
            body_md=body.body_md,
            trigger_ctx=body.trigger_ctx,
            tags=body.tags,
            source_session_id=body.source_session_id,
        )
        return {"ok": True, "data": row}
    finally:
        conn.close()


@router.patch("/rules/{rule_id}")
def patch_rule(
    rule_id: str,
    body: RulePatchBody,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    conn = connect()
    try:
        row = playbook_repo.update_rule(conn, rule_id, owner_id, fields)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="rule not found")
    return {"ok": True, "data": row}


@router.delete("/rules/{rule_id}")
def retire_rule(rule_id: str, owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    conn = connect()
    try:
        row = playbook_repo.retire_rule(conn, rule_id, owner_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="rule not found")
    return {"ok": True, "data": row}


@router.get("/notes")
def list_notes(
    owner_id: str = Depends(require_owner),
    symbol: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    conn = connect()
    try:
        rows = playbook_repo.list_notes(conn, owner_id=owner_id, symbol=symbol, limit=min(limit, 100))
        return {"ok": True, "data": {"rows": rows, "count": len(rows)}}
    finally:
        conn.close()


@router.post("/notes")
def create_note(body: NoteCreateBody, owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    conn = connect()
    try:
        row = playbook_repo.create_note(
            conn,
            owner_id=owner_id,
            note_md=body.note_md,
            tags=body.tags,
            symbols=body.symbols,
            source_session_id=body.source_session_id,
        )
        return {"ok": True, "data": row}
    finally:
        conn.close()


@router.get("/cases")
def list_cases(owner_id: str = Depends(require_owner), limit: int = 50) -> dict[str, Any]:
    conn = connect()
    try:
        rows = playbook_repo.list_cases(conn, owner_id=owner_id, limit=min(limit, 100))
        return {"ok": True, "data": {"rows": rows, "count": len(rows)}}
    finally:
        conn.close()


@router.post("/cases")
def create_case(body: CaseCreateBody, owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    conn = connect()
    try:
        row = playbook_repo.create_case(
            conn,
            owner_id=owner_id,
            lessons_md=body.lessons_md,
            trade_ref=body.trade_ref,
            outcome=body.outcome,
            tags=body.tags,
            related_rule_ids=body.related_rule_ids,
        )
        return {"ok": True, "data": row}
    finally:
        conn.close()


@router.post("/cases/from_bridge")
def create_case_from_bridge(
    body: CaseFromBridgeBody,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    conn = connect()
    try:
        row = playbook_repo.create_case_from_bridge(
            conn,
            owner_id=owner_id,
            bridge_event_id=body.bridge_event_id,
            external_reply_md=body.external_reply_md,
            outcome=body.outcome,
            tags=body.tags,
        )
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="bridge event not found")
    return {"ok": True, "data": row}


@router.get("/search")
def search_playbook(
    q: str,
    owner_id: str = Depends(require_owner),
    limit: int = 20,
) -> dict[str, Any]:
    """Keyword search across rules and notes (RS-KB5 fallback)."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="q required")
    conn = connect()
    try:
        data = playbook_repo.search_keyword(conn, owner_id=owner_id, query=q, limit=min(limit, 50))
        return {"ok": True, "data": data}
    finally:
        conn.close()


@router.get("/triggers")
def list_playbook_triggers(
    symbol: str,
    date_param: date | None = Query(None, alias="date"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """Analyze C.2 — scenario trigger event-log for a symbol/date.

    Reads ``features.stock_signal_playbook_trigger_intraday`` ordered by trigger_at.
    """
    sym = symbol.strip().upper()
    conn = connect()
    cols = (
        "symbol",
        "trade_date",
        "scenario_key",
        "trigger_at",
        "satisfied",
        "condition_snapshot",
        "computed_at",
    )
    try:
        clauses = ["symbol = %s"]
        params: list[Any] = [sym]
        if date_param is not None:
            clauses.append("trade_date = %s")
            params.append(date_param)
        sql = f"""
            SELECT {', '.join(cols)}
            FROM features.stock_signal_playbook_trigger_intraday
            WHERE {' AND '.join(clauses)}
            ORDER BY trigger_at ASC
            LIMIT %s
        """
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = cur.fetchall() or []
        rows: list[dict[str, Any]] = []
        for r in raw:
            if isinstance(r, dict):
                item = dict(r)
            else:
                item = {cols[i]: r[i] for i in range(min(len(cols), len(r)))}
            td = item.get("trade_date")
            if isinstance(td, date):
                item["trade_date"] = td.isoformat()
            for ts_key in ("trigger_at", "computed_at"):
                ts = item.get(ts_key)
                if isinstance(ts, datetime):
                    item[ts_key] = ts.isoformat()
            rows.append(item)
        return {
            "ok": True,
            "data": {
                "symbol": sym,
                "date": date_param.isoformat() if date_param else None,
                "rows": rows,
                "count": len(rows),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        conn.close()


def dominant_scenario_suffix(scenario_key: str) -> str | None:
    """Extract bull/bear/rangy/squeeze from ``dominant:*`` scenario keys."""
    if not scenario_key.startswith(_DOMINANT_PREFIX):
        return None
    suffix = scenario_key[len(_DOMINANT_PREFIX) :].strip().lower()
    return suffix if suffix in _SCENARIO_KEYS else None


def evaluate_dominant_hit(scenario_suffix: str, fwd_return: float) -> bool | None:
    """Return whether forward return matches the dominant scenario direction."""
    if scenario_suffix == "bull":
        return fwd_return > 0
    if scenario_suffix == "bear":
        return fwd_return < 0
    if scenario_suffix == "rangy":
        return abs(fwd_return) < 0.02
    if scenario_suffix == "squeeze":
        return abs(fwd_return) < 0.015
    return None


@router.get("/hit-rate")
def playbook_hit_rate(
    symbol: str = Query(..., min_length=1, max_length=32),
    window_days: int = Query(30, ge=1, le=365),
    horizon: int = Query(5, ge=1, le=60),
) -> dict[str, Any]:
    """Analyze E.3 — dominant-scenario trigger hit-rate vs forward return."""
    sym = symbol.strip().upper()
    conn = connect()
    cols = ("symbol", "trade_date", "scenario_key", "trigger_at", "satisfied")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {', '.join(cols)}
                FROM features.stock_signal_playbook_trigger_intraday
                WHERE symbol = %s
                  AND satisfied = true
                  AND scenario_key LIKE 'dominant:%%'
                  AND trade_date >= CURRENT_DATE - (%s::integer)
                ORDER BY trigger_at ASC
                """,
                (sym, window_days),
            )
            raw = cur.fetchall() or []

        by_scenario: dict[str, dict[str, int | float | None]] = {
            key: {"n": 0, "hits": 0, "rate": None} for key in sorted(_SCENARIO_KEYS)
        }
        rows: list[dict[str, Any]] = []
        evaluated_count = 0
        hit_count = 0

        for r in raw:
            if isinstance(r, dict):
                item = dict(r)
            else:
                item = {cols[i]: r[i] for i in range(min(len(cols), len(r)))}

            scenario_key = str(item.get("scenario_key") or "")
            suffix = dominant_scenario_suffix(scenario_key)
            td = item.get("trade_date")
            trade_date_obj = td if isinstance(td, date) else None
            if trade_date_obj is None and td is not None:
                trade_date_obj = date.fromisoformat(str(td)[:10])

            fwd: float | None = None
            hit: bool | None = None
            if trade_date_obj is not None:
                fwd = _fwd_return(conn, sym, trade_date_obj, horizon)
            if suffix is not None and fwd is not None:
                hit = evaluate_dominant_hit(suffix, fwd)
                evaluated_count += 1
                bucket = by_scenario[suffix]
                bucket["n"] = int(bucket["n"]) + 1
                if hit:
                    hit_count += 1
                    bucket["hits"] = int(bucket["hits"]) + 1

            trigger_at = item.get("trigger_at")
            if isinstance(trigger_at, datetime):
                trigger_at = trigger_at.isoformat()
            trade_date_out = trade_date_obj.isoformat() if trade_date_obj else td
            rows.append(
                {
                    "trigger_at": trigger_at,
                    "trade_date": trade_date_out,
                    "scenario_key": scenario_key,
                    "fwd_return": fwd,
                    "hit": hit,
                }
            )

        for bucket in by_scenario.values():
            n = int(bucket["n"])
            hits = int(bucket["hits"])
            bucket["rate"] = round(hits / n, 4) if n > 0 else None

        trigger_count = len(rows)
        hit_rate = round(hit_count / evaluated_count, 4) if evaluated_count > 0 else None

        return {
            "ok": True,
            "data": {
                "symbol": sym,
                "window_days": window_days,
                "horizon": horizon,
                "trigger_count": trigger_count,
                "evaluated_count": evaluated_count,
                "hit_count": hit_count,
                "hit_rate": hit_rate,
                "by_scenario": by_scenario,
                "rows": rows,
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        conn.close()


__all__ = ["router", "dominant_scenario_suffix", "evaluate_dominant_hit"]
