"""What the Copilot did today, read as standing.

The Autopilot has a standing (trust, next run, purse, memos). The Copilot —
level 2, the models working when asked — had none: its brief lives on one
page, its sessions in a drawer, its spend behind ``/usage`` and its chat
approvals in a ledger nobody lists. This assembles them so the Research home
can show the three postures side by side with the same honesty.

Everything here is a read; no model is called.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from bifrost_research.copilot.rate_limit import get_usage, usage_to_dict

logger = logging.getLogger(__name__)

RECENT_SESSIONS = 3


def _today_utc() -> str:
    return datetime.now(UTC).date().isoformat()


def _is_today(iso: Any, today: str) -> bool:
    return isinstance(iso, str) and iso[:10] == today


def brief_headline(markdown: Any) -> str:
    """The first sentence of the digest, without its heading marks."""
    if not isinstance(markdown, str):
        return ""
    for line in markdown.splitlines():
        text = line.strip()
        if not text or text.startswith(("#", "|", "-", "*", "`")):
            continue
        return text[:200]
    return ""


def brief_today(conn: Any, today: str) -> dict[str, Any] | None:
    from bifrost_research.repositories import ai_draft as draft_repo

    try:
        rows = draft_repo.list_drafts(conn, status=None, kind="daily_digest", limit=5)
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot standing: digest read failed: %s", exc)
        return None
    for r in rows:
        if not _is_today(r.get("created_at"), today):
            continue
        payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        return {
            "draft_id": r.get("id"),
            "status": r.get("status"),
            "created_at": r.get("created_at"),
            "headline": brief_headline(payload.get("markdown")),
            "model": payload.get("model"),
        }
    return None


def sessions_today(conn: Any, owner_id: str, today: str) -> dict[str, Any]:
    from bifrost_research.repositories import copilot_session as session_repo

    try:
        rows = session_repo.list_recent(conn, owner_id=owner_id, limit=50)
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot standing: sessions read failed: %s", exc)
        return {"today": 0, "recent": []}
    recent = [
        {
            "id": str(r.get("id")),
            "title": r.get("title") or "(untitled)",
            "updated_at": r.get("updated_at"),
            "model": r.get("model"),
            "turns": len(r.get("messages") or []) if isinstance(r.get("messages"), list) else None,
        }
        for r in rows[:RECENT_SESSIONS]
    ]
    return {
        "today": sum(1 for r in rows if _is_today(r.get("updated_at"), today)),
        "recent": recent,
    }


def approvals_today(conn: Any, today: str) -> dict[str, int]:
    """Chat-originated writes by ledger status, today: what the Copilot asked
    to do, what was allowed, what ran."""
    from bifrost_research.repositories import ai_action_log as log_repo

    out = {"proposed": 0, "approved": 0, "executed": 0, "rejected": 0, "error": 0}
    for status in out:
        try:
            rows = log_repo.list_actions(conn, status=status, limit=100)
        except Exception as exc:  # noqa: BLE001
            logger.warning("copilot standing: action log (%s) failed: %s", status, exc)
            continue
        out[status] = sum(1 for r in rows if _is_today(r.get("created_at"), today))
    return out


def usage_today(conn: Any, owner_id: str) -> dict[str, Any]:
    from bifrost_research.repositories import copilot_bridge as bridge_repo

    out = usage_to_dict(get_usage())
    try:
        out.update(bridge_repo.usage_stats_today(conn, owner_id=owner_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot standing: bridge usage failed: %s", exc)
        out.update({"bridge_count_today": 0, "bridge_tokens_today": 0, "bridge_cost_usd_today": 0.0})
    return out


def copilot_standing(conn: Any, *, owner_id: str) -> dict[str, Any]:
    today = _today_utc()
    return {
        "day_utc": today,
        "brief": brief_today(conn, today),
        "sessions": sessions_today(conn, owner_id, today),
        "approvals": approvals_today(conn, today),
        "usage": usage_today(conn, owner_id),
    }


__all__ = ["approvals_today", "brief_headline", "brief_today", "copilot_standing", "sessions_today", "usage_today"]
