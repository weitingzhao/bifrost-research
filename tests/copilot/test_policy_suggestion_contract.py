"""Wave Z audit — policy_suggestion whitelist consistency (D-Loop-Cleanup).

Two whitelists exist for the LLM policy_suggestion flow:

* ``plan_llm.POLICY_SUGGESTION_KEYS`` — Pydantic ``LLMPlanResponse`` filter
  and ``suggestion.policy_suggestion_from_plan`` diff filter.
* ``objective_repo.POLICY_SUGGESTION_WHITELIST`` — final barrier when
  Owner approves the Decision Inbox draft
  (``api/agents.py::approve_draft`` + ``patch_policy_json``).

They **must** stay in lockstep — a key added to one but not the other
would either be silently dropped at approval time (spurious noise in
Decision Inbox) or written to ``policy_json`` even though the LLM has
no schema to produce it (defense-in-depth violation).

This contract test fails loudly if the two frozensets drift.  Do not
paper over a failure — update *both* constants in the same change.
"""

from __future__ import annotations

from bifrost_research.copilot.harness import plan_llm
from bifrost_research.repositories import objective as obj_repo


def test_policy_suggestion_whitelists_are_consistent() -> None:
    plan_keys = plan_llm.POLICY_SUGGESTION_KEYS
    repo_keys = obj_repo.POLICY_SUGGESTION_WHITELIST

    assert isinstance(plan_keys, frozenset)
    assert isinstance(repo_keys, frozenset)
    assert plan_keys == repo_keys, (
        "policy_suggestion whitelist drift detected — update both sides.\n"
        f"  plan_llm.POLICY_SUGGESTION_KEYS:          {sorted(plan_keys)}\n"
        f"  objective_repo.POLICY_SUGGESTION_WHITELIST: {sorted(repo_keys)}\n"
        f"  in plan only: {sorted(plan_keys - repo_keys)}\n"
        f"  in repo only: {sorted(repo_keys - plan_keys)}"
    )


def test_policy_suggestion_whitelist_is_non_empty() -> None:
    """Empty whitelist would silently pass every LLM key — guard against typos."""
    assert plan_llm.POLICY_SUGGESTION_KEYS, "whitelist is empty"
    assert obj_repo.POLICY_SUGGESTION_WHITELIST, "whitelist is empty"
