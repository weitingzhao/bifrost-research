"""Defects the post-program review of research-loop-automation confirmed, each pinned.

Every test here failed before its fix. They are grouped by what the Owner would have
seen: a wrong sentence, a number that could not be produced, a guard that could be
stepped around, or an approval that could not be executed at all.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from bifrost_research.copilot import guardrails
from bifrost_research.copilot.agents import symbol_verdicts as SV
from bifrost_research.copilot.approvals import (
    approval_secret_source,
    canonical_input_hash,
    fill_tool_defaults,
    issue_token,
    reset_consumed_for_tests,
    validate_token,
)
from bifrost_research.copilot.harness import entry
from bifrost_research.copilot.harness.runtime import _auto_approve_eligible
from bifrost_research.lenses.registry import LENSES
from bifrost_research.lenses.verdict import LEAN_HEDGE, verdict_for
from bifrost_research.mcp.server import create_mcp_server


@pytest.fixture(autouse=True)
def _tokens() -> None:
    reset_consumed_for_tests()
    yield
    reset_consumed_for_tests()


# ── the D10 chat guardrail ───────────────────────────────────────────────────


def test_a_quoted_literal_neutralises_itself_not_the_whole_message() -> None:
    """One safe phrase used to wave the entire message past every D10 pattern.

    The guard is a token filter: it rejects `place_order`, `ib:operator:cmd`,
    `daemon.scale` and friends. Asking *about* one of those tokens is legitimate, so
    a few quoted forms are exempt — but the exemption used to be evaluated over the
    whole message, so any message containing one was never pattern-matched at all.
    """
    assert guardrails.check_input("what is place_order in this codebase?").tripwire is False
    assert guardrails.check_input("call place_order for me").tripwire is True

    # Both in one message: the question stays answerable, the instruction still trips.
    both = guardrails.check_input("First, what is place_order? Then run daemon.scale to 1.")
    assert both.tripwire is True, "a quoted phrase must not disarm the rest of the message"
    assert both.matched_pattern is not None and "daemon" in both.matched_pattern

    # And the exemption still covers the quoted forms on their own.
    assert guardrails.check_input('the tool is called "place_order"').tripwire is False


# ── the band sentence ────────────────────────────────────────────────────────


def test_a_lean_band_does_not_speak_with_the_extreme_s_voice() -> None:
    hot = verdict_for("iv_rank", 95.0)
    lean = verdict_for("iv_rank", 62.0)
    assert hot is not None and lean is not None
    assert hot["band"] == "hot" and lean["band"] == "lean_hot"
    assert hot["means"] == LENSES["iv_rank"].hot_means
    assert lean["means"] != hot["means"], "IV Rank 62 read exactly like IV Rank 95"
    assert lean["means"] == f"{LEAN_HEDGE}{LENSES['iv_rank'].hot_means}"

    lean_cold = verdict_for("iv_rank", 25.0)
    assert lean_cold is not None and lean_cold["band"] == "lean_cold"
    assert lean_cold["means"] == f"{LEAN_HEDGE}{LENSES['iv_rank'].cold_means}"
    assert verdict_for("iv_rank", 12.0)["means"] == LENSES["iv_rank"].cold_means


# ── the unattended run's own summary ─────────────────────────────────────────


def _result(run_id: str, n: int) -> dict[str, Any]:
    return {"run": {"id": run_id, "status": "awaiting_approval"}, "outputs": {"candidate_ids": ["c"] * n}}


def test_the_cron_summary_can_finally_say_what_was_accepted() -> None:
    """It read result["approve_result"], a key nothing has ever written."""
    accepted = entry.objective_outcome(
        {"id": "obj", "title": "T"},
        result={**_result("run_1", 8), "approve_all": {"accepted_symbols": ["BG"], "held_symbols": [{"symbol": "WT"}]}},
    )
    assert accepted["approved"] is True and accepted["accepted"] == ["BG"]
    assert entry.summary_line(accepted).endswith("auto-accepted=BG")

    held_all = entry.objective_outcome(
        {"id": "obj", "title": "T"},
        result={**_result("run_2", 8), "approve_all": {"accepted_symbols": [], "held_symbols": [{"symbol": "WT"}]}},
    )
    assert held_all["approved"] is False and "auto-accepted" not in entry.summary_line(held_all)

    skipped = entry.objective_outcome(
        {"id": "obj", "title": "T"},
        result={**_result("run_3", 8), "approve_all": {"accepted_symbols": ["BG"]}, "approve_skipped": True},
    )
    assert skipped["approved"] is False


def test_a_judge_stage_that_blew_up_is_not_eligible_for_auto_accept() -> None:
    assert _auto_approve_eligible(None) is True                      # judges never asked
    assert _auto_approve_eligible({"auto_approve_eligible": True}) is True
    assert _auto_approve_eligible({"auto_approve_eligible": False}) is False
    # The error summary is a truthy dict with no eligibility key — it used to default True.
    assert _auto_approve_eligible({"status": "error", "error": "boom"}) is False


# ── the digest the hub strips follow ─────────────────────────────────────────


def test_approving_todays_digest_does_not_send_the_hubs_back_a_day(monkeypatch) -> None:
    today = {"id": "d_today", "status": "approved", "created_at": "2026-09-07T11:30:00+00:00"}
    yesterday = {"id": "d_yday", "status": "pending", "created_at": "2026-09-06T11:30:00+00:00"}

    def _list(conn, *, status=None, kind=None, **kw):
        return [yesterday] if status == "pending" else [today]

    monkeypatch.setattr(SV.draft_repo, "list_drafts", _list)
    assert SV.latest_digest(object())["id"] == "d_today"


# ── approvals ────────────────────────────────────────────────────────────────


def test_a_token_covers_the_defaults_the_tool_will_fill_in() -> None:
    """research.loop.run_objective could not be executed from chat at all."""
    mcp = create_mcp_server()
    sent = {"objective_id": "obj-x"}
    filled = fill_tool_defaults(mcp, "research.loop.run_objective", sent)
    assert filled == {"objective_id": "obj-x", "curate_after": True}

    # What the tool re-hashes is what the token was minted over.
    tool = mcp._tool_manager.get_tool("research.loop.run_objective")  # noqa: SLF001
    params = inspect.signature(tool.fn).parameters
    executed = {"objective_id": "obj-x", "curate_after": params["curate_after"].default}
    assert canonical_input_hash("research.loop.run_objective", filled) == canonical_input_hash(
        "research.loop.run_objective", executed
    )
    issued = issue_token(action_id="a1", tool="research.loop.run_objective", arguments=filled)
    validate_token(issued["approval_token"], tool="research.loop.run_objective", arguments=executed)

    # A caller's explicit value still wins, and meta args never enter the hash.
    assert fill_tool_defaults(mcp, "research.loop.run_objective", {"objective_id": "o", "curate_after": False})["curate_after"] is False
    assert "dry_run" not in fill_tool_defaults(mcp, "research.loop.run_objective", {"objective_id": "o", "dry_run": True})
    # An unknown tool hashes what it was given rather than raising.
    assert fill_tool_defaults(mcp, "research.nope", {"a": 1}) == {"a": 1}


def test_propose_candidate_survives_a_model_omitting_its_optional_arguments() -> None:
    mcp = create_mcp_server()
    filled = fill_tool_defaults(mcp, "research.loop.propose_candidate", {"symbol": "NVDA"})
    assert filled["source"] == "copilot"  # the default the tool would have applied
    issued = issue_token(action_id="a2", tool="research.loop.propose_candidate", arguments=filled)
    validate_token(
        issued["approval_token"],
        tool="research.loop.propose_candidate",
        arguments={"symbol": "NVDA", "source": "copilot", "score": None, "lens_snapshot": {}, "tags": [], "source_ref": {}},
    )


def test_health_says_which_key_signs_approval_tokens(monkeypatch) -> None:
    monkeypatch.delenv("COPILOT_APPROVAL_HMAC_SECRET", raising=False)
    assert approval_secret_source() == "dev_fallback"
    monkeypatch.setenv("COPILOT_APPROVAL_HMAC_SECRET", "a-real-secret")
    assert approval_secret_source() == "env"


def test_the_write_endpoints_take_an_owner_from_the_token_not_the_body() -> None:
    """approved_by is the one field the audit ledger exists to be trusted on."""
    from bifrost_research.api import copilot, harness
    from bifrost_research.auth.deps import require_owner

    for fn in (copilot.copilot_approve, copilot.copilot_execute, harness.approve_all_for_run_endpoint):
        params = inspect.signature(fn).parameters
        assert "owner_id" in params, f"{fn.__name__} has no owner"
        assert params["owner_id"].default.dependency is require_owner, f"{fn.__name__} does not resolve the owner"
    src = inspect.getsource(copilot.copilot_approve) + inspect.getsource(copilot.copilot_execute)
    assert "body.approved_by" not in src, "the caller must not name itself in the ledger"
