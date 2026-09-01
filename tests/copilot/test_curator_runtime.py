"""CuratorRun + batch gate tests — Wave LO-1 / LO-4."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from bifrost_research.copilot.curator.batch_token import issue_batch_pass, validate_batch_pass
from bifrost_research.copilot.harness.trust_gate import trust_l0_research_loop_batch


def test_batch_pass_roundtrip():
    token = issue_batch_pass("run_abc123", ttl_sec=120)
    assert validate_batch_pass(token, "run_abc123")
    assert not validate_batch_pass(token, "run_other")


def test_curator_run_skips_agent(monkeypatch):
    monkeypatch.setenv("BIFROST_CURATOR_SKIP_AGENT", "1")
    conn = MagicMock()
    run = {
        "id": "run_test1",
        "objective_id": "obj-1",
        "status": "awaiting_approval",
        "outputs": {"draft_ids": ["d1"]},
    }
    obj = {"id": "obj-1", "title": "T", "policy_json": {}}

    with patch(
        "bifrost_research.copilot.curator.runtime.obj_repo.get_run",
        return_value=run,
    ), patch(
        "bifrost_research.copilot.curator.runtime.obj_repo.get_objective",
        return_value=obj,
    ), patch(
        "bifrost_research.copilot.curator.runtime.obj_repo.patch_run_outputs",
        return_value=run,
    ) as patch_out:
        from bifrost_research.copilot.curator.runtime import run_curator_for_run

        result = run_curator_for_run(conn, "run_test1")
        assert result["curator_trace"]["status"] == "skipped"
        patch_out.assert_called_once()


def test_trust_l0_requires_batch_mode(monkeypatch):
    monkeypatch.delenv("BIFROST_LOOP_BATCH_MODE", raising=False)
    assert trust_l0_research_loop_batch() is False


def test_trust_l0_override(monkeypatch):
    monkeypatch.setenv("BIFROST_LOOP_BATCH_MODE", "1")
    monkeypatch.setenv("BIFROST_LOOP_TRUST_L0_OVERRIDE", "1")
    assert trust_l0_research_loop_batch() is True


def test_trust_l0_from_platform(monkeypatch):
    monkeypatch.setenv("BIFROST_LOOP_BATCH_MODE", "1")
    monkeypatch.delenv("BIFROST_LOOP_TRUST_L0_OVERRIDE", raising=False)

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "entries": [
                    {"skill_id": "research-loop-batch", "effective_level": "L0"},
                ]
            }

    with patch("httpx.get", return_value=Resp()):
        assert trust_l0_research_loop_batch() is True
