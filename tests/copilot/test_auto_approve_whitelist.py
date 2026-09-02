"""What an unattended run may approve on its own is a governance boundary.

The Loop can run headless once research-loop-batch holds L0 trust. At that point
this frozenset is the only thing standing between the model and a draft it
applies without a human reading it, so widening it is a decision, not a tidy-up.
"""

from __future__ import annotations

from bifrost_research.copilot.harness.batch import RESEARCH_AUTO_APPROVE_KINDS
from bifrost_research.repositories.ai_draft import _ALLOWED_KINDS


def test_the_set_is_exactly_this() -> None:
    """Pinned deliberately. A new entry has to be argued for in a diff."""
    assert RESEARCH_AUTO_APPROVE_KINDS == frozenset(
        {"candidate_batch", "hypothesis_suggestion", "eod_verdict"}
    )


def test_policy_suggestion_is_never_auto_approved() -> None:
    """Approving one merges into objective.policy_json (api/agents.py).

    That rewrites the strategy governing every later run. Policy templates are
    editable data now, so changing the strategy is a deliberate act with an
    author — not something a batch run does on its way past.
    """
    assert "policy_suggestion" not in RESEARCH_AUTO_APPROVE_KINDS


def test_order_intent_is_never_auto_approved() -> None:
    """No handler today, so approving it only flips a status.

    It stays out because of what the name means, not what it currently does: the
    day a handler appears, a whitelist containing it would arm that path with no
    diff to review. D10 blocks order placement; this blocks drifting toward it.
    """
    assert "order_intent" not in RESEARCH_AUTO_APPROVE_KINDS


def test_every_entry_is_a_kind_that_can_actually_exist() -> None:
    """attach_backtest_evidence sat in this set and was never a draft kind.

    It could not match a row, so it read as an allowance that did nothing. An
    entry that cannot match is worse than a missing one: it looks like coverage.
    """
    unknown = RESEARCH_AUTO_APPROVE_KINDS - _ALLOWED_KINDS
    assert unknown == frozenset(), f"not real draft kinds: {sorted(unknown)}"
