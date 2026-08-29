"""Analyze alerts API — Wave M.

GET /research/alerts
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_ALERT_DAILY

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/alerts", tags=["research-alerts"])

VALID_KINDS = frozenset({"composite_high", "weight_shift", "hit_rate_drop"})


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


@router.get("")
def list_alerts(
    limit: int = Query(20, ge=1, le=200),
    kind: str | None = Query(None),
    days: int = Query(14, ge=1, le=90),
) -> dict[str, Any]:
    if kind and kind not in VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"unsupported kind: {kind}")

    conn = _connect_or_503()
    try:
        cutoff = date.today() - timedelta(days=days)
        where = ["trade_date >= %s"]
        params: list[Any] = [cutoff]
        if kind:
            where.append("kind = %s")
            params.append(kind)
        params.append(limit)
        sql = f"""
            SELECT trade_date, kind, symbol, lens, severity, reason_json, computed_at
            FROM {TABLE_STOCK_SIGNAL_ALERT_DAILY}
            WHERE {' AND '.join(where)}
            ORDER BY trade_date DESC, severity DESC, symbol ASC
            LIMIT %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            raw = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.exception("list_alerts failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass

    items = []
    for r in raw:
        reason = r.get("reason_json")
        if isinstance(reason, str):
            try:
                reason = json.loads(reason)
            except Exception:
                reason = {"raw": reason}
        td = r.get("trade_date")
        ca = r.get("computed_at")
        items.append(
            {
                "trade_date": td.isoformat() if isinstance(td, date) else td,
                "kind": r.get("kind"),
                "symbol": r.get("symbol") or None,
                "lens": r.get("lens") or None,
                "severity": r.get("severity"),
                "reason": reason,
                "computed_at": ca.isoformat() if hasattr(ca, "isoformat") else ca,
            }
        )
    return _ok({"count": len(items), "items": items})


__all__ = ["router", "VALID_KINDS"]
