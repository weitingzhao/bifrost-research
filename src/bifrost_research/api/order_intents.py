"""Order Intent Bridge API — Wave O.

Routes:
    GET  /research/order-intents
    POST /research/order-intents/{id}/expire

Advisory only — D10 BLOCKED. Does not place orders.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.copilot.harness.order_intent_schema import OrderIntent
from bifrost_research.db.conn import connect
from bifrost_research.repositories import ai_action_log as action_repo
from bifrost_research.repositories import ai_draft as draft_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/order-intents", tags=["research-order-intents"])

KIND = "order_intent"


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


class OrderIntentCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: OrderIntent
    generated_by: str = Field(default="harness")


@router.get("")
def list_order_intents(
    status: str | None = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        rows = draft_repo.list_drafts(
            conn,
            status=status,
            kind=KIND,
            limit=limit,
        )
    finally:
        conn.close()
    return _ok(
        {
            "items": rows,
            "count": len(rows),
            "advisory": True,
            "d10": "BLOCKED",
        }
    )


@router.post("")
def create_order_intent(body: OrderIntentCreate) -> dict[str, Any]:
    """Create pending order_intent draft (propose only)."""
    conn = _connect_or_503()
    try:
        payload = body.intent.to_payload()
        action = action_repo.insert_action(
            conn,
            action_kind="order_intent",
            action_source=body.generated_by,
            input_payload=payload,
            output_payload=None,
            status="proposed",
        )
        draft = draft_repo.insert_draft(
            conn,
            kind=KIND,
            payload=payload,
            scope=f"hypothesis:{body.intent.hypothesis_id}",
            generated_by=body.generated_by,
            linked_action_id=action["id"],
            expires_at=body.intent.expiry_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
    return _ok({"draft": draft, "action": action, "advisory": True, "d10": "BLOCKED"})


@router.post("/{draft_id}/expire")
def expire_order_intent(draft_id: str) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        draft = draft_repo.get_draft(conn, draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="order intent not found")
        if draft.get("kind") != KIND:
            raise HTTPException(status_code=400, detail="not an order_intent draft")
        updated = draft_repo.update_draft_status(conn, draft_id, status="expired")
    finally:
        conn.close()
    return _ok(updated)
