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
    min_source_hit_rate: float | None = None,
) -> dict[str, Any]:
    """Approve what the run produced — every candidate through the leash (D3).

    A candidate_batch is not one decision: each name passes ``leash.accept_gate``
    on its own (judges agree, validate did not block, evidence measured, the
    source's settled hit rate clears ``min_source_hit_rate``). The names that
    pass become hypotheses; the rest stay on the draft with their reasons, and
    the draft stays pending for the Owner. Other whitelisted kinds approve as
    before. D10 BLOCKED — a hypothesis, never an order.
    """
    from bifrost_research.api.agents import _promote_candidate_batch, apply_draft_approval
    from bifrost_research.copilot.harness.leash import DEFAULT_MIN_SOURCE_HIT_RATE, split_batch
    from bifrost_research.copilot.harness.validate_hook import run_validate_hooks_for_run
    from bifrost_research.repositories import ai_action_log as action_repo

    knob = DEFAULT_MIN_SOURCE_HIT_RATE if min_source_hit_rate is None else float(min_source_hit_rate)

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
    partial: list[str] = []
    held: list[dict[str, Any]] = []
    executed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    hypothesis_ids: list[str] = []
    accepted_symbols: list[str] = []
    held_symbols: list[dict[str, Any]] = []

    whitelist = kinds_whitelist

    for did in draft_ids:
        draft = draft_repo.get_draft(conn, did)
        if draft is None or draft.get("status") != "pending":
            continue
        kind = str(draft.get("kind") or "")
        if whitelist and kind not in whitelist:
            continue
        payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else {}
        # Never auto-approve policy_suggestion / order_intent (whitelist already
        # excludes them). A candidate_batch goes through the leash, name by name.
        if kind == "candidate_batch":
            items = [i for i in (payload.get("items") or []) if isinstance(i, dict)]
            split = split_batch(items, min_source_hit_rate=knob)
            leash = {
                "min_source_hit_rate": knob,
                "accepted": split["accepted"],
                "held": split["held"],
                "decided_by": approved_by,
            }
            accepted_symbols.extend(a["symbol"] for a in split["accepted"])
            held_symbols.extend({"draft_id": did, **h} for h in split["held"])
            patched = {**payload, "leash": leash}
            if not split["accepted"]:
                held.append({"draft_id": did, "reason": "leash_held_all", "held": len(split["held"])})
                _patch_payload_quietly(conn, did, patched)
                continue
            if split["held"]:
                # Partial: promote the names that passed; the draft stays pending
                # with the held names and their reasons for the Owner.
                try:
                    promoted = _promote_candidate_batch(
                        conn,
                        draft_id=did,
                        payload={**patched, "items": [i for i in items if str(i.get("id")) in split["accepted_ids"]]},
                    )
                    _patch_payload_quietly(conn, did, patched)
                    partial.append(did)
                    held.append({"draft_id": did, "reason": "leash_held_some", "held": len(split["held"])})
                    executed.append({"draft_id": did, "partial": True, **promoted})
                    for h in promoted.get("hypotheses") or []:
                        if isinstance(h, dict) and h.get("id"):
                            hypothesis_ids.append(str(h["id"]))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("partial accept of draft %s failed: %s", did, exc)
                    errors.append({"draft_id": did, "detail": str(exc)})
                continue
            draft = {**draft, "payload": patched}
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

    # Wave 4 — optional headless curator after successful auto-approve (playbook draft).
    curator_after: dict[str, Any] | None = None
    if approved and hypothesis_ids:
        try:
            from bifrost_research.copilot.curator.runtime import run_curator_for_run

            curator_after = run_curator_for_run(conn, run_id, skip_agent=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-approve curator failed for %s: %s", run_id, exc)
            curator_after = {"error": str(exc)[:200]}

    if approved and not held:
        obj_repo.update_run_status(conn, run_id, status="completed")
    # Anything held keeps the run awaiting_approval so the Owner sees it in the Inbox.

    if accepted_symbols or held_symbols:
        try:
            action_repo.insert_action(
                conn,
                action_kind="loop_auto_accept",
                action_source="loop_batch",
                input_payload={"run_id": run_id, "min_source_hit_rate": knob, "decided_by": approved_by},
                output_payload={"accepted": accepted_symbols, "held": held_symbols},
                status="executed",
            )
        except Exception as exc:  # noqa: BLE001 — the ledger row must not sink the accept
            logger.warning("loop_auto_accept ledger row failed for %s: %s", run_id, exc)
            rollback_quietly(conn)

    return {
        "approved": approved,
        "partial": partial,
        "held": held,
        "count": len(approved),
        "held_count": len(held),
        "accepted_symbols": accepted_symbols,
        "accepted_count": len(accepted_symbols),
        "held_symbols": held_symbols,
        "held_symbol_count": len(held_symbols),
        "leash": {"min_source_hit_rate": knob},
        "executed": executed,
        "errors": errors,
        "validate": validate_result,
        "hypothesis_ids": hypothesis_ids,
        "curator_after_approve": curator_after,
        "advisory": "D10 BLOCKED — auto-approve is research drafts only, never orders",
    }


def _patch_payload_quietly(conn: _Connection, draft_id: str, payload: dict[str, Any]) -> None:
    try:
        draft_repo.patch_draft_payload(conn, draft_id, payload)
    except Exception as exc:  # noqa: BLE001 — the reasons are for the Owner; losing them must not lose the run
        logger.warning("leash note on draft %s failed: %s", draft_id, exc)
        rollback_quietly(conn)


def rollback_quietly(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass


__all__ = ["RESEARCH_AUTO_APPROVE_KINDS", "approve_all_for_run"]
