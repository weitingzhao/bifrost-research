"""A rejected batch pass has to say which rejection it was.

Three unrelated situations — no curator run in context, a token the model
mis-copied, and an expired pass — all surfaced as `400: malformed approval
token`. The headless curator hit one of them unattended, spent its whole run
reasoning about a governance problem it could not identify, and neither could
anyone reading the log afterwards.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from bifrost_research.copilot.curator import batch_token as bt
from bifrost_research.mcp.tools._write_common import require_approval_or_error

RUN = "run-abc"


@pytest.fixture(autouse=True)
def _clean_env() -> Any:
    prev = os.environ.get("BIFROST_CURATOR_RUN_ID")
    yield
    if prev is None:
        os.environ.pop("BIFROST_CURATOR_RUN_ID", None)
    else:
        os.environ["BIFROST_CURATOR_RUN_ID"] = prev


def _gate(token: str | None, *, run_env: str | None) -> str:
    os.environ.pop("BIFROST_CURATOR_RUN_ID", None)
    if run_env:
        os.environ["BIFROST_CURATOR_RUN_ID"] = run_env
    out = require_approval_or_error(
        dry_run=False, approval_token=token, tool="t", arguments={}
    )
    return "" if out is None else str(out["error"])


def test_valid_pass_is_accepted() -> None:
    assert _gate(bt.issue_batch_pass(RUN), run_env=RUN) == ""


def test_missing_curator_context_says_so() -> None:
    err = _gate(bt.issue_batch_pass(RUN), run_env=None)
    assert "no curator run in context" in err
    assert "malformed" not in err


def test_altered_token_says_the_signature_failed() -> None:
    tok = bt.issue_batch_pass(RUN)
    err = _gate(tok[:-1] + ("0" if tok[-1] != "0" else "1"), run_env=RUN)
    assert "signature does not verify" in err


def test_truncated_token_is_distinguished_from_an_altered_one() -> None:
    tok = bt.issue_batch_pass(RUN)
    err = _gate(tok.rsplit("|", 1)[0], run_env=RUN)
    assert "truncated or reshaped" in err


def test_wrong_run_names_both_runs() -> None:
    err = _gate(bt.issue_batch_pass("other-run"), run_env=RUN)
    assert "other-run" in err and RUN in err


def test_expired_pass_reports_how_stale_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    tok = bt.issue_batch_pass(RUN, ttl_sec=30)
    real = bt.time.time
    monkeypatch.setattr(bt.time, "time", lambda: real() + 3600)
    err = _gate(tok, run_env=RUN)
    assert "expired" in err
    assert "s ago" in err


def test_the_three_causes_are_all_distinguishable() -> None:
    """The regression this exists for: they used to be one string."""
    tok = bt.issue_batch_pass(RUN)
    messages = {
        _gate(tok, run_env=None),
        _gate(tok[:-1] + "0", run_env=RUN),
        _gate(bt.issue_batch_pass("other"), run_env=RUN),
    }
    assert len(messages) == 3


def test_a_non_batch_token_still_uses_the_per_tool_validator() -> None:
    """The batch branch must not swallow ordinary Owner tokens.

    The env var is set only inside the curator's own in-process agent run, so an
    Owner token never arrives while it is set — outside that window the per-tool
    validator stays the reporter.
    """
    err = _gate("not-a-batch-pass", run_env=None)
    assert "malformed approval token" in err


def test_dry_run_needs_no_token_at_all() -> None:
    assert (
        require_approval_or_error(
            dry_run=True, approval_token=None, tool="t", arguments={}
        )
        is None
    )


def test_wrong_token_during_a_curator_run_names_the_transcription_problem() -> None:
    """The case that actually fired: curator running, token is not its pass.

    Reported as an ordinary `malformed approval token`, which points at the
    token's format rather than at the model having failed to carry it across.
    """
    err = _gate("some-owner-token", run_env=RUN)
    assert "curator run is in context" in err
    assert "no curator-batch prefix" in err


def test_wrong_token_without_a_curator_run_is_still_a_token_problem() -> None:
    """Outside a curator run the per-tool validator is the right reporter."""
    err = _gate("some-owner-token", run_env=None)
    assert "malformed approval token" in err
