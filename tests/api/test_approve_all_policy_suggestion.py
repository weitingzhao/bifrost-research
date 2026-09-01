"""Approve-all must reuse Inbox apply_draft_approval (policy merge)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app


@pytest.fixture(autouse=True)
def _health_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bifrost_research.api.health.run_startup_schema_guard",
        lambda: None,
    )
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


class _Env:
    def __init__(self) -> None:
        self.drafts: dict[str, dict[str, Any]] = {}
        self.patch_policy_captured: dict[str, Any] = {}
        self.patch_policy_return: dict[str, Any] | None = None
        self.update_draft_calls: list[dict[str, Any]] = []
        self.get_objective_return: dict[str, Any] | None = None
        self.run: dict[str, Any] | None = None


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()
    fake_conn = MagicMock()

    monkeypatch.setattr("bifrost_research.api.harness.connect", lambda: fake_conn)
    monkeypatch.setattr("bifrost_research.api.agents.connect", lambda: fake_conn)

    monkeypatch.setattr(
        "bifrost_research.api.harness.obj_repo.get_run",
        lambda conn, run_id: e.run,
    )
    monkeypatch.setattr(
        "bifrost_research.api.harness.obj_repo.update_run_status",
        lambda conn, run_id, *, status: {"id": run_id, "status": status},
    )
    # approve-all moved out of api/harness.py into copilot/harness/batch.py, which
    # is where draft_repo is bound now — api.harness no longer imports it.
    monkeypatch.setattr(
        "bifrost_research.copilot.harness.batch.draft_repo.get_draft",
        lambda conn, draft_id: e.drafts.get(draft_id),
    )
    monkeypatch.setattr(
        "bifrost_research.api.agents.draft_repo.get_draft",
        lambda conn, draft_id: e.drafts.get(draft_id),
    )

    def _update_draft(conn, draft_id, *, status):
        e.update_draft_calls.append({"draft_id": draft_id, "status": status})
        row = dict(e.drafts.get(draft_id) or {"id": draft_id})
        row["status"] = status
        e.drafts[draft_id] = row
        return row

    monkeypatch.setattr(
        "bifrost_research.api.agents.draft_repo.update_draft_status", _update_draft
    )
    monkeypatch.setattr(
        "bifrost_research.api.agents.objective_repo.get_objective",
        lambda conn, oid: e.get_objective_return,
    )

    def _patch_policy(conn, oid, patch):
        e.patch_policy_captured = {"objective_id": oid, "patch": patch}
        return e.patch_policy_return

    monkeypatch.setattr(
        "bifrost_research.api.agents.objective_repo.patch_policy_json",
        _patch_policy,
    )
    monkeypatch.setattr(
        "bifrost_research.api.agents.action_repo.insert_action",
        lambda conn, **k: {"id": "aal_new", **k},
    )
    monkeypatch.setattr(
        "bifrost_research.api.agents.action_repo.update_action_status",
        lambda conn, action_id, **k: {"id": action_id, **k},
    )
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_approve_all_merges_policy_suggestion(client: TestClient, env: _Env) -> None:
    env.drafts["aid_ps_1"] = {
        "id": "aid_ps_1",
        "kind": "policy_suggestion",
        "status": "pending",
        "payload": {
            "objective_id": "obj-abc",
            "suggestion": {"min_hit_rate": 0.7, "preset": "momentum"},
        },
        "linked_action_id": "aal_orig",
    }
    env.run = {
        "id": "run-1",
        "outputs": {"draft_ids": ["aid_ps_1"]},
    }
    env.get_objective_return = {"id": "obj-abc", "policy_json": {"min_hit_rate": 0.55}}
    env.patch_policy_return = {
        "id": "obj-abc",
        "policy_json": {"min_hit_rate": 0.7, "preset": "momentum"},
    }

    resp = client.post("/research/objective-runs/run-1/approve-all")
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["approved"] == ["aid_ps_1"]
    assert body["count"] == 1
    assert env.patch_policy_captured == {
        "objective_id": "obj-abc",
        "patch": {"min_hit_rate": 0.7, "preset": "momentum"},
    }
    assert env.update_draft_calls[0]["status"] == "approved"
    executed = body["executed"][0]
    assert executed["kind"] == "policy_suggestion"
    assert executed["applied_suggestion"]["preset"] == "momentum"


def test_approve_all_skips_non_pending(client: TestClient, env: _Env) -> None:
    env.drafts["aid_done"] = {
        "id": "aid_done",
        "kind": "policy_suggestion",
        "status": "approved",
        "payload": {"objective_id": "obj-abc", "suggestion": {"min_hit_rate": 0.7}},
    }
    env.run = {"id": "run-2", "outputs": {"draft_ids": ["aid_done"]}}
    resp = client.post("/research/objective-runs/run-2/approve-all")
    assert resp.status_code == 200
    assert resp.json()["data"]["approved"] == []
    assert env.patch_policy_captured == {}
