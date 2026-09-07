"""A token issued over what the Owner saw must validate against what the tool executes."""

from __future__ import annotations

import pytest

from bifrost_research.copilot.approvals import ApprovalError, canonical_input_hash, issue_token, reset_consumed_for_tests, validate_token


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_consumed_for_tests()
    yield
    reset_consumed_for_tests()


def test_defaults_the_tool_fills_in_do_not_read_as_tampering() -> None:
    seen = {"symbol": "NVDA", "source": "copilot", "score": 62.0, "tags": ["d4"]}
    executed = {"symbol": "NVDA", "source": "copilot", "score": 62.0, "lens_snapshot": {}, "tags": ["d4"], "source_ref": {}}
    assert canonical_input_hash("t", seen) == canonical_input_hash("t", executed)
    assert canonical_input_hash("t", {"a": None, "b": {"c": []}}) == canonical_input_hash("t", {})
    issued = issue_token(action_id="aal_hash", tool="research.loop.propose_candidate", arguments=seen)
    validate_token(issued["approval_token"], tool="research.loop.propose_candidate", arguments=executed)


def test_a_changed_argument_still_reads_as_tampering() -> None:
    issued = issue_token(action_id="aal_hash2", tool="research.loop.propose_candidate", arguments={"symbol": "NVDA", "score": 62.0})
    with pytest.raises(ApprovalError) as ei:
        validate_token(issued["approval_token"], tool="research.loop.propose_candidate", arguments={"symbol": "NVDA", "score": 99.0})
    assert ei.value.status == 400
    assert canonical_input_hash("t", {"tags": ["a"]}) != canonical_input_hash("t", {"tags": ["b"]})
