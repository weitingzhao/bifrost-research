"""Playbook REST API — RS-KB3."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.auth.deps import require_owner
from bifrost_research.db.conn import connect
from bifrost_research.repositories import playbook as playbook_repo

router = APIRouter(prefix="/research/playbook", tags=["research-playbook"])


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


__all__ = ["router"]
