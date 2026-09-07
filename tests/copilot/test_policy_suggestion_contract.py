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


def test_owner_whitelist_is_a_superset_that_adds_only_the_judging_and_planning_knobs():
    # A model may not propose switching its own planner or judges off; the Owner
    # may. Everything the model may propose, the Owner may too.
    from bifrost_research.repositories import objective as obj_repo

    extra = obj_repo.OWNER_POLICY_WHITELIST - obj_repo.POLICY_SUGGESTION_WHITELIST
    assert obj_repo.POLICY_SUGGESTION_WHITELIST <= obj_repo.OWNER_POLICY_WHITELIST
    assert extra == {"triage", "persona_evaluate", "use_llm_plan", "llm_model", "seed_symbols"}


def test_nested_policy_groups_merge_key_by_key():
    # Editing triage.deep_judge_top_n from the page must not wipe triage.model,
    # and editing resolution.horizon_days must not reset its thresholds.
    from bifrost_research.repositories.objective import _deep_merge_policy_patch

    cur = {"triage": {"enabled": True, "model": "gpt-4o-mini", "deep_judge_top_n": 0}, "resolution": {"horizon_days": 20, "validate_excess": 0.03}}
    out = _deep_merge_policy_patch(cur, {"triage": {"deep_judge_top_n": 3}, "resolution": {"horizon_days": 10}})
    assert out["triage"] == {"enabled": True, "model": "gpt-4o-mini", "deep_judge_top_n": 3}
    assert out["resolution"] == {"horizon_days": 10, "validate_excess": 0.03}
