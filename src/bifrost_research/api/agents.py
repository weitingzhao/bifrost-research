"""Morning / EOD agents + draft inbox API — Wave RS-E3.

Routes:
    POST /research/agents/morning/run
    POST /research/agents/eod/run
    GET  /research/drafts
    POST /research/drafts/{id}/approve
    POST /research/drafts/{id}/dismiss

D-RS-E-e: agents only create drafts; approve applies hypothesis mutations.
D10: no trading paths.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.auth.deps import require_owner
from bifrost_research.db.conn import connect
from bifrost_research.repositories import ai_action_log as action_repo
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import candidate_pool as cand_repo
from bifrost_research.repositories import hypothesis as hyp_repo
from bifrost_research.repositories import objective as objective_repo

logger = logging.getLogger(__name__)

agents_router = APIRouter(prefix="/research/agents", tags=["research-agents"])
drafts_router = APIRouter(prefix="/research/drafts", tags=["research-drafts"])


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _err(msg: str, status: int = 400) -> None:
    raise HTTPException(status_code=status, detail=msg)


class RunBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dry_run: bool = False


@agents_router.post("/morning/run")
def run_morning(body: RunBody | None = None) -> dict[str, Any]:
    from bifrost_research.copilot.agents.morning_prep import run_morning_prep

    dry = bool(body.dry_run) if body else False
    if dry or os.environ.get("BIFROST_MORNING_AGENT_DRY_RUN", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
    ):
        result = run_morning_prep(dry_run=True)
        return _ok(result)
    try:
        result = run_morning_prep(dry_run=False)
    except Exception as exc:
        logger.exception("morning agent failed")
        _err(f"morning agent failed: {exc}", 500)
    return _ok(result)


@agents_router.post("/eod/run")
def run_eod(body: RunBody | None = None) -> dict[str, Any]:
    from bifrost_research.copilot.agents.eod_review import run_eod_review

    dry = bool(body.dry_run) if body else False
    if dry or os.environ.get("BIFROST_EOD_AGENT_DRY_RUN", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
    ):
        result = run_eod_review(dry_run=True)
        return _ok(result)
    try:
        result = run_eod_review(dry_run=False)
    except Exception as exc:
        logger.exception("eod agent failed")
        _err(f"eod agent failed: {exc}", 500)
    return _ok(result)


_MANUAL_DRAFT_KINDS = frozenset(
    {"hypothesis_suggestion", "morning_brief", "eod_verdict"}
)


class CreateDraftBody(BaseModel):
    """Owner-manual draft — Wave X (Loop Usability).

    Only three kinds are exposed to manual creation via API/UI:
    hypothesis_suggestion / morning_brief / eod_verdict.
    Loop / harness kinds (candidate_batch, hypothesis_draft, decision_draft,
    order_intent, playbook_*) must go through their originating agent/harness
    flow so provenance stays clean.
    """

    model_config = ConfigDict(extra="ignore")

    kind: str
    title: str
    summary: str
    hypothesis_id: str | None = None
    symbols: list[str] | None = None


@drafts_router.post("")
def create_draft(
    body: CreateDraftBody,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    kind = str(body.kind or "").strip()
    if kind not in _MANUAL_DRAFT_KINDS:
        _err(
            f"kind {kind!r} not allowed for manual creation; "
            f"allowed: {sorted(_MANUAL_DRAFT_KINDS)}",
            400,
        )
    title = str(body.title or "").strip()
    summary = str(body.summary or "").strip()
    if not title:
        _err("title is required", 400)
    if not summary:
        _err("summary is required", 400)

    hyp_id = (body.hypothesis_id or "").strip() or None
    symbols = [s.strip().upper() for s in (body.symbols or []) if isinstance(s, str) and s.strip()]

    payload: dict[str, Any] = {
        "title": title,
        "summary": summary,
        "markdown": summary,
        "manual": True,
        "created_by": owner_id,
    }
    if hyp_id:
        payload["hypothesis_id"] = hyp_id
    if symbols:
        payload["symbols"] = symbols
    if kind == "morning_brief":
        payload["create_hypothesis"] = False

    scope = f"hypothesis:{hyp_id}" if hyp_id else f"manual:{kind}"
    conn = connect()
    try:
        action = action_repo.insert_action(
            conn,
            action_kind="draft_manual_create",
            action_source="owner_manual",
            input_payload={"kind": kind, "hypothesis_id": hyp_id},
            output_payload={"payload": payload},
            status="proposed",
        )
        draft = draft_repo.insert_draft(
            conn,
            kind=kind,
            payload=payload,
            scope=scope,
            generated_by="owner_manual",
            linked_action_id=action["id"],
        )
        return _ok({"draft": draft, "action": action})
    except ValueError as exc:
        _err(str(exc), 400)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("manual draft create failed")
        _err(f"create failed: {exc}", 500)
    finally:
        conn.close()


@drafts_router.get("")
def list_drafts(
    status: str | None = Query(default="pending"),
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    conn = connect()
    try:
        rows = draft_repo.list_drafts(
            conn, status=status, kind=kind, limit=limit, offset=offset
        )
        pending = draft_repo.count_pending(conn)
        return _ok(
            {
                "rows": rows,
                "count": len(rows),
                "pending_count": pending,
                "limit": limit,
                "offset": offset,
            }
        )
    except ValueError as exc:
        _err(str(exc), 400)
    except Exception as exc:
        logger.exception("list drafts failed")
        _err(f"list drafts failed: {exc}", 500)
    finally:
        conn.close()


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    approved_by: str = Field(default="owner")


def _promote_candidate_batch(
    conn: Any,
    *,
    draft_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Promote open pool rows + create lightweight hypotheses.

    Missing / non-open candidates are skipped into ``skipped`` — never raised.
    D10 BLOCKED — writes research.hypothesis + research.candidate_pool only.
    """
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    batch_title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    run_id = payload.get("run_id")
    promoted: list[dict[str, Any]] = []
    hypotheses: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            skipped.append({"reason": "invalid_item"})
            continue
        cid = str(item.get("id") or item.get("candidate_id") or "").strip()
        symbol = str(item.get("symbol") or "").strip().upper()
        if not cid:
            skipped.append({"symbol": symbol or None, "reason": "missing_id"})
            continue
        try:
            cand = cand_repo.get_candidate(conn, cid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("candidate_batch get %s failed: %s", cid, exc)
            skipped.append({"id": cid, "symbol": symbol or None, "reason": "lookup_failed"})
            continue
        if cand is None:
            skipped.append({"id": cid, "symbol": symbol or None, "reason": "not_found"})
            continue
        if cand.get("status") != "open":
            skipped.append(
                {
                    "id": cid,
                    "symbol": cand.get("symbol") or symbol or None,
                    "reason": f"not_open:{cand.get('status')}",
                }
            )
            continue
        symbol = str(cand.get("symbol") or symbol or "").strip().upper()
        title = " · ".join(p for p in (batch_title, symbol) if p) or cid
        thesis_bits: list[str] = []
        if description:
            thesis_bits.append(description[:400])
        lens = cand.get("lens_snapshot") if isinstance(cand.get("lens_snapshot"), dict) else {}
        flags = lens.get("lens_flags") if isinstance(lens, dict) else None
        if flags:
            thesis_bits.append(f"lens_flags={flags}")
        score = lens.get("composite_score") if isinstance(lens, dict) else item.get("score")
        if score is not None:
            thesis_bits.append(f"composite_score={score}")
        thesis = " · ".join(thesis_bits) or f"Promoted from candidate_batch {draft_id}"
        try:
            hyp = hyp_repo.create_hypothesis(
                conn,
                title=title[:200],
                thesis=thesis[:4000],
                symbols=[symbol] if symbol else [],
                tags=["harness", "candidate_batch"],
                origin_page="candidate_batch_approve",
                origin_ref={
                    "draft_id": draft_id,
                    "run_id": run_id,
                    "candidate_id": cid,
                },
            )
            promoted_row = cand_repo.promote_candidate(
                conn, cid, hypothesis_id=hyp["id"]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("candidate_batch item %s skipped: %s", cid, exc)
            skipped.append({"id": cid, "symbol": symbol or None, "reason": str(exc)})
            continue
        hypotheses.append(hyp)
        if promoted_row is None:
            skipped.append(
                {
                    "id": cid,
                    "symbol": symbol or None,
                    "reason": "promote_missed",
                    "hypothesis_id": hyp["id"],
                }
            )
            continue
        promoted.append(promoted_row)

    return {
        "promoted": promoted,
        "hypotheses": hypotheses,
        "skipped": skipped,
        "note": (
            f"promoted {len(promoted)} candidates; "
            f"created {len(hypotheses)} hypotheses"
        ),
    }


def apply_draft_approval(
    conn: Any,
    draft: dict[str, Any],
    *,
    approved_by: str,
    owner_id: str,
) -> dict[str, Any]:
    """Shared Inbox / Approve-all side effects. Raises HTTPException on hard errors.

    D10 BLOCKED — no Trade DB / ib:operator:cmd writes.
    """
    if draft is None:
        _err("draft not found", 404)
    if draft.get("status") != "pending":
        _err(f"draft status is {draft.get('status')}, expected pending", 409)

    draft_id = str(draft["id"])
    payload = draft.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    executed: dict[str, Any] = {"draft_id": draft_id, "kind": draft["kind"]}
    hyp_result = None

    if draft["kind"] == "eod_verdict":
        hyp_id = payload.get("hypothesis_id") or draft.get("scope")
        proposed = payload.get("proposed_status")
        conclusion = payload.get("rationale") or payload.get("markdown")
        if hyp_id and proposed in {"active", "validated", "rejected", "archived"}:
            fields: dict[str, Any] = {"status": proposed}
            if conclusion:
                fields["conclusion"] = str(conclusion)[:4000]
            hyp_result = hyp_repo.patch_hypothesis(conn, str(hyp_id), fields)
            executed["hypothesis"] = hyp_result
        else:
            executed["hypothesis"] = None
            executed["note"] = "no status applied (missing proposed_status or id)"

    elif draft["kind"] == "morning_brief":
        if payload.get("create_hypothesis") is True:
            title = str(payload.get("title") or "").strip()
            thesis = str(payload.get("thesis") or "").strip()
            if title and thesis:
                hyp_result = hyp_repo.create_hypothesis(
                    conn,
                    title=title,
                    thesis=thesis,
                    symbols=payload.get("symbols") or [],
                    tags=list(payload.get("tags") or []) + ["morning_brief"],
                    origin_page="cockpit_inbox",
                    origin_ref={"draft_id": draft_id},
                )
                executed["hypothesis"] = hyp_result
            else:
                executed["note"] = "create_hypothesis true but title/thesis missing"
        else:
            executed["note"] = "morning_brief approved (note only; no hypothesis write)"

    elif draft["kind"] == "playbook_rule":
        from bifrost_research.repositories import agent_persona as persona_repo
        from bifrost_research.repositories import playbook as playbook_repo

        agent_owner = str(payload.get("agent_owner") or "shared")
        rule = playbook_repo.create_rule(
            conn,
            owner_id=owner_id,
            title=str(payload.get("title") or "Untitled rule"),
            category=str(payload.get("category") or "general"),
            body_md=str(payload.get("body_md") or ""),
            trigger_ctx=payload.get("trigger_ctx") if isinstance(payload.get("trigger_ctx"), dict) else {},
            tags=list(payload.get("tags") or []),
            source_session_id=payload.get("source_session_id"),
            agent_owner=agent_owner,
        )
        executed["playbook_rule"] = rule
        persona_diff = payload.get("persona_diff")
        if isinstance(persona_diff, dict) and persona_diff and agent_owner != "shared":
            persona_row = persona_repo.apply_preference_diff(
                conn,
                owner_id,
                agent_owner,
                persona_diff,
            )
            executed["persona"] = persona_row

    elif draft["kind"] == "playbook_note":
        from bifrost_research.repositories import playbook as playbook_repo

        note = playbook_repo.create_note(
            conn,
            owner_id=owner_id,
            note_md=str(payload.get("note_md") or ""),
            tags=list(payload.get("tags") or []),
            symbols=list(payload.get("symbols") or []),
            source_session_id=payload.get("source_session_id"),
        )
        executed["playbook_note"] = note

    elif draft["kind"] == "policy_suggestion":
        # Wave Y.3 A1 — merge LLM-proposed policy_json patch onto the
        # objective.  Whitelist filtering happens in repositories layer;
        # unknown keys are silently dropped.  D10 BLOCKED — this only
        # touches research.objective, never Trade DB or IB operator.
        objective_id = payload.get("objective_id")
        suggestion = payload.get("suggestion")
        if not objective_id or not isinstance(objective_id, str):
            _err("policy_suggestion draft missing objective_id", 400)
        if not isinstance(suggestion, dict) or not suggestion:
            _err("policy_suggestion draft missing suggestion dict", 400)
        existing = objective_repo.get_objective(conn, objective_id)
        if existing is None:
            _err(f"objective {objective_id!r} not found", 404)
        updated_obj = objective_repo.patch_policy_json(
            conn, objective_id, suggestion
        )
        executed["objective"] = updated_obj
        executed["applied_suggestion"] = {
            k: v
            for k, v in suggestion.items()
            if k in objective_repo.POLICY_SUGGESTION_WHITELIST
        }

    elif draft["kind"] == "candidate_batch":
        executed.update(_promote_candidate_batch(conn, draft_id=draft_id, payload=payload))

    else:
        # Loop / harness kinds (order_intent, decision_draft, …):
        # mark approved only — advisory; no Trade DB / ib:operator:cmd (D10).
        executed["note"] = f"approved {draft['kind']} (advisory pass-through; no side-effect write)"

    updated = draft_repo.update_draft_status(conn, draft_id, status="approved")
    linked = draft.get("linked_action_id")
    action_row = None
    if linked:
        action_row = action_repo.update_action_status(
            conn,
            linked,
            status="executed",
            approved_by=approved_by,
            executed_result=executed,
        )
    else:
        action_row = action_repo.insert_action(
            conn,
            action_kind="draft_approve",
            action_source="user_chat",
            input_payload={"draft_id": draft_id},
            output_payload=executed,
            status="executed",
        )
        action_repo.update_action_status(
            conn,
            action_row["id"],
            status="executed",
            approved_by=approved_by,
            executed_result=executed,
        )

    return {
        "draft": updated,
        "action": action_row,
        "executed": executed,
    }


@drafts_router.post("/{draft_id}/approve")
def approve_draft(
    draft_id: str,
    body: ApproveBody | None = None,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    approved_by = (body.approved_by if body else owner_id) or owner_id
    conn = connect()
    try:
        draft = draft_repo.get_draft(conn, draft_id)
        if draft is None:
            _err("draft not found", 404)
        return _ok(
            apply_draft_approval(
                conn, draft, approved_by=approved_by, owner_id=owner_id
            )
        )
    except HTTPException:
        raise
    except ValueError as exc:
        _err(str(exc), 400)
    except Exception as exc:
        logger.exception("approve draft failed")
        _err(f"approve failed: {exc}", 500)
    finally:
        conn.close()


@drafts_router.post("/{draft_id}/dismiss")
def dismiss_draft(draft_id: str, body: ApproveBody | None = None) -> dict[str, Any]:
    approved_by = (body.approved_by if body else "owner") or "owner"
    conn = connect()
    try:
        draft = draft_repo.get_draft(conn, draft_id)
        if draft is None:
            _err("draft not found", 404)
        if draft["status"] != "pending":
            _err(f"draft status is {draft['status']}, expected pending", 409)

        updated = draft_repo.update_draft_status(conn, draft_id, status="dismissed")
        linked = draft.get("linked_action_id")
        action_row = None
        if linked:
            action_row = action_repo.update_action_status(
                conn,
                linked,
                status="rejected",
                approved_by=approved_by,
                executed_result={"draft_id": draft_id, "dismissed": True},
            )
        else:
            action_row = action_repo.insert_action(
                conn,
                action_kind="draft_dismiss",
                action_source="user_chat",
                input_payload={"draft_id": draft_id},
                output_payload={"dismissed": True},
                status="rejected",
            )

        return _ok({"draft": updated, "action": action_row})
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("dismiss draft failed")
        _err(f"dismiss failed: {exc}", 500)
    finally:
        conn.close()
