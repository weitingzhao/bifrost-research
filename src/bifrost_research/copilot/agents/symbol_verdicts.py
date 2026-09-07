"""What Copilot and the Loop have said about one symbol — research-loop-automation D4.

The hub views show the lenses; this is the other half of the page: the day's
digest lines for the symbol (lens bands, whether the Loop proposed it, what
the leash decided, whether the judges split, whether a hypothesis on it was
resolved) and every chat-side proposal about it with its approval state —
candidates the Copilot proposed, hypotheses it created, decision / order
drafts waiting, and the approval ledger behind them.

Read-only, from the same tables the Inbox reads, so a chip on a hub view and
the card in the Inbox cannot disagree. D10 BLOCKED — states, never orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bifrost_research.repositories import ai_action_log as action_repo
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import candidate_pool as cand_repo
from bifrost_research.repositories import hypothesis as hyp_repo

ADVISORY = "D10 BLOCKED — states of research proposals; nothing here is an order."
DRAFT_KINDS = ("decision_draft", "order_intent", "hypothesis_suggestion")
MAX_PROPOSALS = 12
CANDIDATE_STATE = {"open": "proposed", "promoted": "accepted", "dismissed": "dismissed", "expired": "expired"}
HYPOTHESIS_STATE = {"active": "active", "validated": "validated", "rejected": "rejected", "archived": "archived"}
DRAFT_STATE = {"pending": "awaiting approval", "approved": "approved", "dismissed": "dismissed", "expired": "expired"}
ACTION_STATE = {"proposed": "previewed", "approved": "approved", "executed": "executed", "rejected": "rejected", "error": "failed"}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _symbols_of(payload: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    single = payload.get("symbol")
    if isinstance(single, str) and single.strip():
        out.add(single.strip().upper())
    many = payload.get("symbols")
    if isinstance(many, list):
        out.update(str(s).strip().upper() for s in many if isinstance(s, str) and s.strip())
    return out


def latest_digest(conn: Any) -> dict[str, Any] | None:
    for status in ("pending", "approved"):
        rows = draft_repo.list_drafts(conn, status=status, kind="daily_digest", limit=1)
        if rows:
            return rows[0]
    return None


def digest_view(digest: dict[str, Any] | None, symbol: str) -> dict[str, Any] | None:
    """The digest's lines about one symbol, or None when the digest never mentions it."""
    if not digest:
        return None
    payload = _dict(digest.get("payload"))
    lenses = [
        {k: ln.get(k) for k in ("lens", "band", "value", "means", "as_of")}
        for ln in (_dict(payload.get("exhibits")).get(symbol) or [])
        if isinstance(ln, dict)
    ]
    proposed = any(str(c.get("symbol") or "").upper() == symbol for c in payload.get("candidates") or [] if isinstance(c, dict))
    batches = []
    for b in payload.get("batches") or []:
        if not isinstance(b, dict) or symbol not in [str(s).upper() for s in b.get("candidates") or []]:
            continue
        held = next((h for h in b.get("held") or [] if isinstance(h, dict) and str(h.get("symbol") or "").upper() == symbol), None)
        batches.append(
            {
                "run_id": b.get("run_id"),
                "objective_title": b.get("objective_title"),
                "status": b.get("status"),
                "auto_accepted": symbol in [str(s).upper() for s in b.get("auto_accepted") or []],
                "held_reasons": list(held.get("reasons") or []) if held else [],
            }
        )
    dissent = next((d for d in payload.get("dissents") or [] if isinstance(d, dict) and str(d.get("symbol") or "").upper() == symbol), None)
    resolution = next(
        (r for r in payload.get("resolutions") or [] if isinstance(r, dict) and symbol in [str(s).upper() for s in r.get("symbols") or []]),
        None,
    )
    if not lenses and not proposed and not batches and not dissent and not resolution:
        return None
    parts = [f"{ln['lens']} {ln['band'] or 'no reading'}" for ln in lenses]
    if batches:
        b = batches[0]
        if b["auto_accepted"]:
            parts.append("auto-accepted by the leash")
        elif b["held_reasons"]:
            parts.append(f"held: {b['held_reasons'][0]}")
        else:
            parts.append(f"proposed by {b['objective_title'] or 'the Loop'}")
    if dissent:
        parts.append("judges split")
    if resolution:
        parts.append(f"hypothesis {resolution.get('status')}")
    return {
        "day": payload.get("day"),
        "draft_id": digest.get("id"),
        "status": digest.get("status"),
        "lenses": lenses,
        "proposed": proposed,
        "batches": batches,
        "dissent": {k: dissent.get(k) for k in ("run_id", "objective_title", "net_stance", "blocked_by_validate", "judges", "wrong_if")} if dissent else None,
        "resolution": {k: resolution.get(k) for k in ("id", "title", "status", "decision", "excess", "by_rule")} if resolution else None,
        "line": " · ".join(parts),
    }


def _candidate_rows(conn: Any, symbol: str) -> list[dict[str, Any]]:
    rows = cand_repo.list_candidates(conn, status=None, symbol=symbol, days=60, limit=10)
    return [
        {
            "kind": "candidate",
            "id": r.get("id"),
            "status": r.get("status"),
            "state": CANDIDATE_STATE.get(str(r.get("status")), str(r.get("status"))),
            "source": r.get("source"),
            "by_copilot": str(r.get("source") or "") == "copilot",
            "score": r.get("score"),
            "hypothesis_id": r.get("hypothesis_id"),
            "created_at": r.get("created_at"),
            "title": f"Candidate · {r.get('source') or '?'}",
        }
        for r in rows
    ]


def _hypothesis_rows(conn: Any, symbol: str) -> list[dict[str, Any]]:
    rows = hyp_repo.list_hypotheses(conn, symbol=symbol, include_retired=True, limit=10)
    out = []
    for r in rows:
        receipt = _dict(r.get("resolution_json"))
        out.append(
            {
                "kind": "hypothesis",
                "id": r.get("id"),
                "status": r.get("status"),
                "state": HYPOTHESIS_STATE.get(str(r.get("status")), str(r.get("status"))),
                "origin_page": r.get("origin_page"),
                "by_copilot": str(r.get("origin_page") or "") in ("copilot", "cockpit_inbox"),
                "by_rule": bool(receipt),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
                "title": r.get("title"),
            }
        )
    return out


def _draft_rows(conn: Any, symbol: str) -> list[dict[str, Any]]:
    out = []
    for kind in DRAFT_KINDS:
        for d in draft_repo.list_drafts(conn, status=None, kind=kind, limit=50):
            payload = _dict(d.get("payload"))
            if symbol not in _symbols_of(payload):
                continue
            out.append(
                {
                    "kind": "draft",
                    "draft_kind": kind,
                    "id": d.get("id"),
                    "status": d.get("status"),
                    "state": DRAFT_STATE.get(str(d.get("status")), str(d.get("status"))),
                    "generated_by": d.get("generated_by"),
                    "by_copilot": True,
                    "created_at": d.get("created_at"),
                    "title": str(payload.get("title") or payload.get("summary") or kind)[:120],
                }
            )
    return out


def _action_rows(conn: Any, symbol: str) -> list[dict[str, Any]]:
    out = []
    for a in action_repo.list_actions(conn, action_source="user_chat", limit=100):
        args = _dict(_dict(a.get("input")).get("arguments"))
        if symbol not in _symbols_of(args):
            continue
        kind = str(a.get("action_kind") or "")
        out.append(
            {
                "kind": "action",
                "id": a.get("id"),
                "tool": kind,
                "status": a.get("status"),
                "state": ACTION_STATE.get(str(a.get("status")), str(a.get("status"))),
                "by_copilot": True,
                "approved_by": a.get("approved_by"),
                "session_id": a.get("session_id"),
                "created_at": a.get("created_at"),
                "title": kind.split(".")[-1].replace("_", " ") if kind else "chat action",
            }
        )
    return out


def _sort_key(row: dict[str, Any]) -> str:
    return str(row.get("created_at") or "")


def build_symbol_verdicts(conn: Any, symbol: str, *, now: datetime | None = None) -> dict[str, Any]:
    sym = (symbol or "").strip().upper()
    now = now or datetime.now(timezone.utc)
    digest = digest_view(latest_digest(conn), sym)
    proposals = sorted(
        [*_candidate_rows(conn, sym), *_hypothesis_rows(conn, sym), *_draft_rows(conn, sym), *_action_rows(conn, sym)],
        key=_sort_key,
        reverse=True,
    )[:MAX_PROPOSALS]
    counts: dict[str, int] = {}
    for p in proposals:
        counts[p["kind"]] = counts.get(p["kind"], 0) + 1
    return {
        "symbol": sym,
        "generated_at": now.isoformat(),
        "digest": digest,
        "proposals": proposals,
        "counts": counts,
        "advisory": ADVISORY,
    }
