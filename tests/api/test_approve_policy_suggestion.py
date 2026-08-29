"""Wave Y.3 A1 — approve policy_suggestion draft → merge onto objective."""

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
        self.get_draft_return: dict[str, Any] | None = None
        self.get_objective_return: dict[str, Any] | None = None
        self.patch_policy_captured: dict[str, Any] = {}
        self.patch_policy_return: dict[str, Any] | None = None
        self.update_draft_captured: dict[str, Any] = {}
        self.update_action_captured: dict[str, Any] = {}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()

    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
    fake_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr("bifrost_research.api.agents.connect", lambda: fake_conn)

    monkeypatch.setattr(
        "bifrost_research.api.agents.draft_repo.get_draft",
        lambda conn, draft_id: e.get_draft_return,
    )

    def _update_draft(conn, draft_id, *, status):
        e.update_draft_captured = {"draft_id": draft_id, "status": status}
        return {"id": draft_id, "status": status}

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

    def _update_action(conn, action_id, **k):
        e.update_action_captured = {"action_id": action_id, **k}
        return {"id": action_id, **k}

    monkeypatch.setattr(
        "bifrost_research.api.agents.action_repo.update_action_status",
        _update_action,
    )

    return e


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    return TestClient(app)


def _draft(payload: dict[str, Any], *, linked_action_id: str | None = None) -> dict[str, Any]:
    return {
        "id": "aid_ps_1",
        "kind": "policy_suggestion",
        "status": "pending",
        "payload": payload,
        "scope": "objective:obj-abc",
        "linked_action_id": linked_action_id,
    }


def test_approve_policy_suggestion_merges_into_objective(
    client: TestClient, env: _Env
) -> None:
    env.get_draft_return = _draft(
        {
            "objective_id": "obj-abc",
            "suggestion": {"min_hit_rate": 0.7, "preset": "momentum"},
            "current_policy": {"min_hit_rate": 0.55},
        },
        linked_action_id="aal_orig",
    )
    env.get_objective_return = {"id": "obj-abc", "policy_json": {"min_hit_rate": 0.55}}
    env.patch_policy_return = {
        "id": "obj-abc",
        "policy_json": {"min_hit_rate": 0.7, "preset": "momentum"},
    }

    resp = client.post("/research/drafts/aid_ps_1/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    executed = body["data"]["executed"]
    assert executed["kind"] == "policy_suggestion"
    assert executed["objective"]["policy_json"] == {
        "min_hit_rate": 0.7,
        "preset": "momentum",
    }
    assert executed["applied_suggestion"] == {
        "min_hit_rate": 0.7,
        "preset": "momentum",
    }
    # patch_policy_json called with full suggestion dict; whitelist filter is
    # inside repositories.objective (unit-tested separately).
    assert env.patch_policy_captured == {
        "objective_id": "obj-abc",
        "patch": {"min_hit_rate": 0.7, "preset": "momentum"},
    }
    # draft transitioned to approved
    assert env.update_draft_captured["status"] == "approved"
    # linked action promoted to executed
    assert env.update_action_captured["status"] == "executed"


def test_approve_policy_suggestion_missing_objective_id_rejected(
    client: TestClient, env: _Env
) -> None:
    env.get_draft_return = _draft({"suggestion": {"min_hit_rate": 0.7}})
    resp = client.post("/research/drafts/aid_ps_1/approve")
    assert resp.status_code == 400
    assert "objective_id" in resp.text


def test_approve_policy_suggestion_missing_suggestion_rejected(
    client: TestClient, env: _Env
) -> None:
    env.get_draft_return = _draft({"objective_id": "obj-x"})
    resp = client.post("/research/drafts/aid_ps_1/approve")
    assert resp.status_code == 400
    assert "suggestion" in resp.text


def test_approve_policy_suggestion_missing_objective_returns_404(
    client: TestClient, env: _Env
) -> None:
    env.get_draft_return = _draft(
        {"objective_id": "obj-gone", "suggestion": {"min_hit_rate": 0.7}}
    )
    env.get_objective_return = None
    resp = client.post("/research/drafts/aid_ps_1/approve")
    assert resp.status_code == 404
    assert "not found" in resp.text


def test_approve_pending_status_only(client: TestClient, env: _Env) -> None:
    env.get_draft_return = {
        **_draft({"objective_id": "obj-abc", "suggestion": {"min_hit_rate": 0.7}}),
        "status": "approved",
    }
    resp = client.post("/research/drafts/aid_ps_1/approve")
    assert resp.status_code == 409
