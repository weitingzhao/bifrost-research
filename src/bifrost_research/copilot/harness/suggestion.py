"""LLM policy_suggestion diff — Wave Y.3 A1 (extracted in Wave Z cleanup).

Given an LLM plan (with an optional ``policy_suggestion``) and the current
objective ``policy_json``, produce the actionable diff that becomes the
payload of a ``policy_suggestion`` Decision Inbox draft.

Only keys in ``plan_llm.POLICY_SUGGESTION_KEYS`` may cross into a draft
(defense in depth — Pydantic already filtered at plan-generation time).
Only keys whose *value* differs from the current policy are surfaced.

D10 BLOCKED — this module builds a proposal dict; the actual write onto
``objective.policy_json`` happens in ``api.agents.approve_draft`` after
Owner approval, via ``repositories.objective.patch_policy_json``.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.copilot.harness import plan_llm


def policy_suggestion_from_plan(
    plan: dict[str, Any],
    current_policy: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return an actionable diff, or ``None`` when nothing changes.

    - ``plan.policy_suggestion`` must be a non-empty dict.
    - Keys outside ``POLICY_SUGGESTION_KEYS`` are ignored (should already
      be filtered by ``LLMPlanResponse``).
    - Keys whose value equals ``current_policy.get(key)`` are dropped.
    - Empty diff → ``None`` (runtime skips draft creation).
    """
    raw = plan.get("policy_suggestion")
    if not isinstance(raw, dict) or not raw:
        return None
    current = current_policy or {}
    diff: dict[str, Any] = {}
    for key in plan_llm.POLICY_SUGGESTION_KEYS:
        if key not in raw:
            continue
        proposed = raw[key]
        if current.get(key) != proposed:
            diff[key] = proposed
    return diff or None
