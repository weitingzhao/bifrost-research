"""The Inbox badge and the Inbox page must count the same queue.

The badge read `pending_memos`, which counts candidate batches for the active
objectives. That is the right number on an objective row and the wrong one on
a link to the Decision Inbox: the badge said three while the page it opened
offered twenty-four calls. One queue, counted two ways — the same failure this
module was already fixed for once, on the folding of repeated batches.

The three rules are the page's own (`harnessDraftHelpers`): a briefing is not
a call, repeats of one batch are one call, and a policy suggestion that would
write nothing is not a call. The third is answered here rather than mirrored
from the frontend, because this side owns both whitelists and is the side that
would do the writing.
"""

from __future__ import annotations

from typing import Any

import pytest

from bifrost_research.copilot.harness import standing


def draft(kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"kind": kind, "payload": payload or {}}


def batch(objective: str, *symbols: str) -> dict[str, Any]:
    return draft(
        "candidate_batch",
        {"objective_id": objective, "items": [{"symbol": s} for s in symbols]},
    )


def count(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> dict[str, int]:
    from bifrost_research.repositories import ai_draft as draft_repo

    monkeypatch.setattr(draft_repo, "list_drafts", lambda *a, **k: rows)
    return standing.pending_decision_calls(object())


# ── what counts as a call ────────────────────────────────────────────────


def test_a_briefing_is_not_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    out = count(monkeypatch, [draft("morning_brief"), draft("eod_verdict"), draft("daily_digest")])
    assert out["calls"] == 0
    assert out["briefings"] == 3


def test_repeats_of_one_batch_are_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [batch("obj-a", "NVDA", "HALO") for _ in range(11)]
    out = count(monkeypatch, rows)
    assert out["calls"] == 1
    assert out["folded"] == 10
    assert out["drafts"] == 11


def test_different_names_are_different_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    out = count(monkeypatch, [batch("obj-a", "NVDA"), batch("obj-a", "HALO")])
    assert out["calls"] == 2


def test_the_same_names_under_another_objective_is_its_own_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = count(monkeypatch, [batch("obj-a", "NVDA"), batch("obj-b", "NVDA")])
    assert out["calls"] == 2


def test_a_batch_with_no_symbols_stands_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    out = count(monkeypatch, [batch("obj-a"), batch("obj-a")])
    assert out["calls"] == 2 and out["folded"] == 0


def test_a_suggestion_that_would_write_nothing_is_not_a_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    same = draft("policy_suggestion", {"suggestion": {"max_candidates": 8}, "current_policy": {"max_candidates": 8}})
    out = count(monkeypatch, [same])
    assert out["calls"] == 0 and out["inert"] == 1


def test_a_suggestion_that_would_write_something_is(monkeypatch: pytest.MonkeyPatch) -> None:
    out = count(
        monkeypatch,
        [draft("policy_suggestion", {"suggestion": {"max_candidates": 10}, "current_policy": {"max_candidates": 8}})],
    )
    assert out["calls"] == 1 and out["inert"] == 0


def test_an_unmodelled_kind_still_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    # By exclusion on purpose: an allowlist would hide exactly the draft a
    # human most needs to see.
    out = count(monkeypatch, [draft("order_intent"), draft("decision_draft"), draft("something_new")])
    assert out["calls"] == 3


def test_a_lookup_failure_reports_zero_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bifrost_research.repositories import ai_draft as draft_repo

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(draft_repo, "list_drafts", boom)
    assert standing.pending_decision_calls(object())["calls"] == 0


# ── the whitelist follows the author ─────────────────────────────────────


def test_an_owner_may_move_a_knob_a_model_may_not() -> None:
    payload = {"manual": True, "suggestion": {"decline_memory": {"enabled": False}}, "current_policy": {}}
    assert standing.policy_suggestion_writes(payload) == 1


def test_the_same_key_from_a_model_writes_nothing() -> None:
    # Approval would drop it, so the Inbox must not offer it as a call.
    payload = {"suggestion": {"decline_memory": {"enabled": False}}, "current_policy": {}}
    assert standing.policy_suggestion_writes(payload) == 0


def test_either_marker_the_endpoint_stamps_means_owner() -> None:
    for marker in ({"manual": True}, {"source": "owner"}):
        payload = {**marker, "suggestion": {"use_llm_plan": True}, "current_policy": {}}
        assert standing.policy_suggestion_writes(payload) == 1


def test_a_key_on_neither_whitelist_never_counts() -> None:
    payload = {"manual": True, "suggestion": {"not_a_policy_key": 1}, "current_policy": {}}
    assert standing.policy_suggestion_writes(payload) == 0
