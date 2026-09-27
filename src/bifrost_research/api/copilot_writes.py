"""Copilot writes HTTP route — the Desk's Writes table.

GET /research/copilot/writes → the chat's write-tool rows, newest first, with their thread
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from bifrost_research.auth.deps import require_owner
from bifrost_research.copilot.writes import chat_writes, window_start
from bifrost_research.db.conn import connect

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/copilot/writes", tags=["research-copilot"])


@router.get("")
def copilot_writes(
    days: int = Query(default=7, ge=1, le=30),
    limit: int = Query(default=50, ge=1, le=200),
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    """What the chat asked to change, one row per write, newest first.

    The last ``days`` UTC days including today. Each row keeps the thread it
    came from so the Desk can open it; ``total`` is counted, not the page size.
    """
    try:
        conn = connect()
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot writes: no database: %s", exc)
        return {
            "ok": True,
            "data": {
                "days": days,
                "since_day_utc": window_start(days),
                "rows": [],
                "total": 0,
                "truncated": False,
                "last_write_at": None,
                "db_ok": False,
            },
        }
    try:
        data = chat_writes(conn, owner_id=owner_id, days=days, limit=limit)
    finally:
        conn.close()
    data["db_ok"] = True
    return {"ok": True, "data": data}
