"""Shared harness batch helpers — Wave LO-3 / LO-4."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import objective as obj_repo

logger = logging.getLogger(__name__)

# Kinds an unattended batch run may approve on its own.
#
# Narrowed to research outputs. Two removals carry the weight:
#
#   policy_suggestion — apply_draft_approval merges it into objective.policy_json
#     (api/agents.py:423). Auto-approving it lets the model rewrite the strategy
#     that governs every later run, unattended. Now that policy templates are
#     editable data, changing the strategy is a deliberate act with an author.
#
#   order_intent — no handler today, so approving it only flips a status. But the
#     name carries order semantics, and the day a handler appears this whitelist
#     would arm it silently. D10 is not the guard here; this is.
#
# decision_draft has no handler either, so its removal changes nothing today.
# attach_backtest_evidence was never a draft kind at all — it is absent from
# ai_draft._ALLOWED_KINDS, so it could never have matched a row. A whitelist
# should list what it means to allow, not carry entries that happened to be
# harmless or, worse, entries that never meant anything.
RESEARCH_AUTO_APPROVE_KINDS = frozenset(
    {
        "candidate_batch",
        "hypothesis_suggestion",
        "eod_verdict",
    }
)


class _Connection(Protocol):
    def cursor(self) -> Any: ...


def approve_all_for_run(
    conn: _Connection,
    run_id: str,
    *,
    approved_by: str = "owner",
    owner_id: str = "owner",
    kinds_whitelist: frozenset[str] | None = None,
    auto_validate: bool = False,
) -> dict[str, Any]:
    from bifrost_research.api.agents import apply_draft_approval
    from bifrost_research.copilot.harness.validate_hook import run_validate_hooks_for_run

    run = obj_repo.get_run(conn, run_id)
    if run is None:
        raise ValueError("run not found")

    outputs = run.get("outputs") or {}
    draft_ids = list(outputs.get("draft_ids") or [])
    curator = outputs.get("curator_trace") or {}
    if isinstance(curator, dict):
        extra = curator.get("new_draft_ids")
        if isinstance(extra, list):
            draft_ids = list(dict.fromkeys(draft_ids + [str(x) for x in extra if x]))

    approved: list[str] = []
    executed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    hypothesis_ids: list[str] = []

    whitelist = kinds_whitelist

    for did in draft_ids:
        draft = draft_repo.get_draft(conn, did)
        if draft is None or draft.get("status") != "pending":
            continue
        kind = str(draft.get("kind") or "")
        if whitelist and kind not in whitelist:
            continue
        try:
            result = apply_draft_approval(
                conn, draft, approved_by=approved_by, owner_id=owner_id
            )
            approved.append(did)
            ex = result.get("executed")
            if isinstance(ex, dict):
                executed.append(ex)
                hyps = ex.get("hypotheses")
                if isinstance(hyps, list):
                    for h in hyps:
                        if isinstance(h, dict) and h.get("id"):
                            hypothesis_ids.append(str(h["id"]))
        except Exception as exc:
            logger.warning("approve draft %s failed: %s", did, exc)
            errors.append({"draft_id": did, "detail": str(exc)})

    validate_result: dict[str, Any] | None = None
    if auto_validate and hypothesis_ids:
        validate_result = run_validate_hooks_for_run(
            conn,
            run_id=run_id,
            hypothesis_ids=hypothesis_ids,
            auto_validate=True,
        )

    obj_repo.update_run_status(conn, run_id, status="completed")
    return {
        "approved": approved,
        "count": len(approved),
        "executed": executed,
        "errors": errors,
        "validate": validate_result,
        "hypothesis_ids": hypothesis_ids,
    }


__all__ = ["RESEARCH_AUTO_APPROVE_KINDS", "approve_all_for_run"]
