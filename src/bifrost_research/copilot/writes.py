"""What the chat asked to change — the Copilot's writes, one row each.

A chat write is a write tool the Owner approved (or refused) on a card in the
Copilot panel: ``research.ai_action_log`` rows with ``action_source =
'user_chat'`` whose kind is one of ``WRITE_TOOL_NAMES``. The same source also
carries the spend ledger (``chat_turn``) and the Decision Inbox's approve /
dismiss clicks (``draft_approve`` / ``draft_dismiss``); neither is something
the chat asked to write, and the scheduled agents write under their own
sources — so the kind set, not the source alone, is what makes a row a chat
write.

Everything here is a read; no model is called.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from bifrost_research.copilot.agents.symbol_verdicts import _dict, _symbols_of
from bifrost_research.mcp.tools._write_common import WRITE_TOOL_NAMES

logger = logging.getLogger(__name__)

CHAT_SOURCE = "user_chat"
STATUSES = ("proposed", "approved", "executed", "rejected", "error")
DEFAULT_DAYS = 7
SUMMARY_MAX = 160
ERROR_MAX = 120

# The noun the table's Kind column shows, per tool.
_KIND_NOUN = {
    "research.hypothesis.create": "hypothesis",
    "research.hypothesis.patch": "hypothesis",
    "research.hypothesis.retire": "hypothesis",
    "research.backtest.run_event_query": "backtest",
    "research.playbook.propose_rule": "rule",
    "research.playbook.propose_note": "note",
    "research.loop.propose_candidate": "candidate",
    "research.loop.promote_to_hypothesis": "hypothesis",
    "research.loop.attach_backtest_evidence": "evidence",
    "research.loop.draft_decision": "decision",
    "research.loop.propose_order_intent": "intent",
    "research.loop.run_objective": "objective",
}

_PATCH_FIELDS = ("title", "thesis", "symbols", "tags", "status", "conclusion")


def window_start(days: int, *, today: date | None = None) -> str:
    """First UTC day of a window of ``days`` days ending today (inclusive)."""
    d = today or datetime.now(UTC).date()
    return (d - timedelta(days=max(1, int(days)) - 1)).isoformat()


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _first_line(value: Any) -> str:
    for line in _text(value).splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line
    return ""


def kind_noun(tool: str) -> str:
    if tool in _KIND_NOUN:
        return _KIND_NOUN[tool]
    tail = tool.rsplit(".", 1)[-1] if tool else ""
    return tail.replace("_", " ") or "write"


def _clip(text: str, n: int = SUMMARY_MAX) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def summarize_change(tool: str, args: dict[str, Any]) -> str:
    """One line saying what the write changes, from the arguments it ran with."""
    syms = sorted(_symbols_of(args))
    sym_tail = f" · {', '.join(syms[:3])}" if syms else ""
    hid = _text(args.get("hypothesis_id"))

    if tool == "research.hypothesis.create":
        text = f"New hypothesis: {_text(args.get('title')) or '(untitled)'}{sym_tail}"
    elif tool == "research.hypothesis.patch":
        changed = []
        for f in _PATCH_FIELDS:
            if args.get(f) in (None, "", []):
                continue
            changed.append(f"status → {_text(args['status'])}" if f == "status" else f)
        text = f"{hid or 'hypothesis'} · {', '.join(changed) or 'no fields'}"
    elif tool == "research.hypothesis.retire":
        text = f"Retire {hid or 'hypothesis'}"
    elif tool == "research.backtest.run_event_query":
        years = args.get("lookback_years")
        text = (
            f"Backtest {_text(args.get('strategy_template')) or 'template'}"
            f" on {_text(args.get('event_kind')) or 'events'}"
            + (f" · {years}y" if years not in (None, "") else "")
            + (f" · for {hid}" if hid else "")
        )
    elif tool == "research.playbook.propose_rule":
        cat = _text(args.get("category"))
        text = f"Rule{f' ({cat})' if cat else ''}: {_text(args.get('title')) or '(untitled)'}"
    elif tool == "research.playbook.propose_note":
        text = f"Note: {_first_line(args.get('note_md')) or '(empty)'}{sym_tail}"
    elif tool == "research.loop.propose_candidate":
        score = args.get("score")
        text = f"Candidate {', '.join(syms) or '(no symbol)'}" + (
            f" · score {score:g}" if isinstance(score, (int, float)) else ""
        )
    elif tool == "research.loop.promote_to_hypothesis":
        title = _text(args.get("title"))
        text = f"Promote {_text(args.get('candidate_id')) or 'candidate'} to a hypothesis" + (
            f": {title}" if title else ""
        )
    elif tool == "research.loop.attach_backtest_evidence":
        text = f"Attach backtest {_text(args.get('backtest_run_id')) or '?'} to {hid or 'hypothesis'}"
    elif tool == "research.loop.draft_decision":
        text = f"{(_text(args.get('verdict')) or 'decision').capitalize()} on {hid or 'hypothesis'}"
    elif tool == "research.loop.propose_order_intent":
        legs = args.get("legs")
        n = len(legs) if isinstance(legs, list) else 0
        text = (
            f"{_text(args.get('strategy_template')) or 'strategy'} intent for {hid or 'hypothesis'}"
            + (f" · {n} leg{'s' if n != 1 else ''}" if n else "")
        )
    elif tool == "research.loop.run_objective":
        text = f"Run objective {_text(args.get('objective_id')) or '?'}"
    else:
        keys = [k for k in args if k not in ("dry_run", "approval_token")]
        text = f"{kind_noun(tool)}{sym_tail}" + (f" · {', '.join(keys[:4])}" if keys else "")
    return _clip(text)


def _outcome(row: dict[str, Any]) -> tuple[bool | None, str | None]:
    """(ran ok, error text) from the ledger's executed_result."""
    result = _dict(row.get("executed_result"))
    ok = result.get("ok") if isinstance(result.get("ok"), bool) else None
    err: str | None = None
    if row.get("status") == "error":
        raw = result.get("error") or _dict(result.get("result")).get("error") or result.get("detail")
        err = _clip(_text(raw), ERROR_MAX) or None
    return ok, err


def shape_write(row: dict[str, Any]) -> dict[str, Any]:
    inp = _dict(row.get("input"))
    tool = _text(row.get("action_kind")) or _text(inp.get("tool_name"))
    args = _dict(inp.get("arguments"))
    ok, err = _outcome(row)
    syms = sorted(_symbols_of(args))
    sid = _text(row.get("session_id")) or None
    return {
        "id": row.get("id"),
        "tool": tool,
        "kind": kind_noun(tool),
        "change": summarize_change(tool, args),
        "symbol": syms[0] if syms else None,
        "session_id": sid,
        "thread_title": (_text(row.get("thread_title")) or None) if sid else None,
        "thread_archived": row.get("thread_status") not in (None, "active") if sid else None,
        "status": row.get("status"),
        "ok": ok,
        "error": err,
        "created_at": row.get("created_at"),
        "executed_at": row.get("executed_at"),
    }


def chat_writes(conn: Any, *, owner_id: str, days: int = DEFAULT_DAYS, limit: int = 50) -> dict[str, Any]:
    from bifrost_research.repositories import ai_action_log as log_repo

    since = window_start(days)
    page = log_repo.list_with_thread(
        conn,
        action_source=CHAT_SOURCE,
        kinds=WRITE_TOOL_NAMES,
        since_day=since,
        owner_id=owner_id,
        limit=limit,
    )
    return {
        "days": int(days),
        "since_day_utc": since,
        "rows": [shape_write(r) for r in page["rows"]],
        "total": page["total"],
        "truncated": page["truncated"],
        "last_write_at": page["last_at"],
    }


def chat_write_counts(conn: Any, *, day: str, owner_id: str | None = None) -> dict[str, int]:
    """Chat writes per status on one UTC day — the Desk's Writes meta."""
    from bifrost_research.repositories import ai_action_log as log_repo

    out = dict.fromkeys(STATUSES, 0)
    until = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    try:
        counted = log_repo.count_by_status(
            conn,
            action_source=CHAT_SOURCE,
            kinds=WRITE_TOOL_NAMES,
            since_day=day,
            until_day=until,
            approved_by=owner_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot writes: status count failed: %s", exc)
        return out
    for status, n in counted.items():
        if status in out:
            out[status] = n
    return out


__all__ = [
    "CHAT_SOURCE",
    "chat_write_counts",
    "chat_writes",
    "kind_noun",
    "shape_write",
    "summarize_change",
    "window_start",
]
