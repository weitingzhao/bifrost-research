"""Weekly policy review — the rules get a proposal from settled outcomes (research-loop-automation D3).

Once a week, each active objective is judged on its own record: the settled
``research.candidate_outcome`` rows of the candidates it proposed (by their
``source_ref.objective_id``) and the judges' verdicts on its latest run. When
that record says the rule is too loose, ``policy_suggestion_from_outcomes``
drafts a tightening as a ``policy_suggestion`` for the Owner — never applied
on its own, never auto-approved (it is outside RESEARCH_AUTO_APPROVE_KINDS).
When the record is fine, the review leaves a ledger row and no draft: an Inbox
card that proposes nothing is noise.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Protocol

from bifrost_research.copilot.harness.suggestion import policy_suggestion_from_outcomes
from bifrost_research.repositories import ai_action_log as action_repo
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import objective as obj_repo

logger = logging.getLogger(__name__)

AGENT_ID = "weekly_policy_review"
ACTION_SOURCE = "weekly_policy_review"
SUGGESTION_SOURCE = "weekly_outcomes"
WINDOW_DAYS = 90
ADVISORY = "D10 BLOCKED — a policy proposal for the Owner; nothing is applied on its own."


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _latest_persona(conn: _Connection, objective_id: str) -> dict[str, Any] | None:
    """The judges' summary from the objective's latest run, if it had one."""
    runs = obj_repo.list_runs(conn, objective_id=objective_id, limit=1)
    if not runs:
        return None
    outputs = runs[0].get("outputs") if isinstance(runs[0].get("outputs"), dict) else {}
    persona = outputs.get("persona_eval")
    return persona if isinstance(persona, dict) else None


def _already_proposed(conn: _Connection, objective_id: str, week: str) -> dict[str, Any] | None:
    rows = draft_repo.list_drafts(conn, status="pending", kind="policy_suggestion", scope=f"objective:{objective_id}", limit=20)
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if payload.get("source") == SUGGESTION_SOURCE and payload.get("week") == week:
            return row
    return None


def review_objective(
    conn: _Connection,
    obj: dict[str, Any],
    *,
    week: str,
    days: int = WINDOW_DAYS,
    force: bool = False,
) -> dict[str, Any]:
    """One objective against its settled record; returns what was proposed, or why not."""
    from bifrost_research.api.candidate_outcome import build_summary

    objective_id = str(obj.get("id") or "")
    policy = obj.get("policy_json") if isinstance(obj.get("policy_json"), dict) else {}
    existing = _already_proposed(conn, objective_id, week)
    if existing and not force:
        return {"objective_id": objective_id, "skipped": True, "reason": "already proposed this week", "draft_id": existing.get("id")}

    summary = build_summary(conn, objective_id=objective_id, days=days)
    persona = _latest_persona(conn, objective_id)
    proposal = policy_suggestion_from_outcomes(persona, current_policy=policy, outcome_summary=summary)
    judged = max((int(h.get("judged") or 0) for h in summary.get("horizons") or []), default=0)
    evidence = {
        "window_days": days,
        "outcome_summary": summary,
        "persona_eval": persona,
        "judged_outcomes": judged,
    }
    if not proposal:
        action_repo.insert_action(
            conn,
            action_kind="weekly_policy_review",
            action_source=ACTION_SOURCE,
            input_payload={"objective_id": objective_id, "week": week, "window_days": days},
            output_payload={"proposed": False, "judged_outcomes": judged, "reason": "settled record does not call for a change"},
            status="executed",
        )
        return {"objective_id": objective_id, "proposed": False, "judged_outcomes": judged}

    action = action_repo.insert_action(
        conn,
        action_kind="weekly_policy_review",
        action_source=ACTION_SOURCE,
        input_payload={"objective_id": objective_id, "week": week, "window_days": days},
        output_payload={"proposed": True, "suggestion": proposal.get("suggestion"), "judged_outcomes": judged},
        status="proposed",
    )
    draft = draft_repo.insert_draft(
        conn,
        kind="policy_suggestion",
        payload={
            "objective_id": objective_id,
            "run_id": None,
            "suggestion": proposal.get("suggestion"),
            "current_policy": policy,
            "source": SUGGESTION_SOURCE,
            "week": week,
            "llm_model": None,
            "llm_reasoning": proposal.get("reasoning"),
            "evidence": evidence,
            "advisory": ADVISORY,
        },
        scope=f"objective:{objective_id}",
        generated_by=AGENT_ID,
        linked_action_id=action["id"],
    )
    return {
        "objective_id": objective_id,
        "proposed": True,
        "draft_id": draft["id"],
        "suggestion": proposal.get("suggestion"),
        "judged_outcomes": judged,
    }


def run_weekly_policy_review(
    conn: _Connection | None = None,
    *,
    day: date | None = None,
    days: int = WINDOW_DAYS,
    force: bool = False,
) -> dict[str, Any]:
    """Every active objective, once a week. Returns ``{ok, week, reviewed, proposed}``."""
    day = day or datetime.now(timezone.utc).date()
    week = week_label(day)
    owns_conn = False
    if conn is None:
        from bifrost_research.db.conn import connect

        conn = connect()
        owns_conn = True
    reviewed: list[dict[str, Any]] = []
    try:
        objectives = obj_repo.list_objectives(conn, status="active", limit=50)
        for obj in objectives:
            try:
                reviewed.append(review_objective(conn, obj, week=week, days=days, force=force))
            except Exception as exc:  # noqa: BLE001 — one objective's failure is its own line
                logger.warning("weekly policy review failed for %s: %s", obj.get("id"), exc)
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                reviewed.append({"objective_id": obj.get("id"), "error": str(exc)[:200]})
        return {
            "ok": True,
            "week": week,
            "window_days": days,
            "objectives": len(objectives),
            "reviewed": reviewed,
            "proposed": [r["draft_id"] for r in reviewed if r.get("proposed")],
            "advisory": ADVISORY,
        }
    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
