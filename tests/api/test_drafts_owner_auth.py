"""Draft decisions and the draft list need a research user.

Dismiss is as final as approve — the draft closes, its action row is rejected
and, for a candidate_batch, the batch's open pool rows are marked dismissed —
so an anonymous caller must not reach it.

The list (``GET /research/drafts``) carries the Loop's pending decisions, their
rationale and the daily digest. Every frontend reader goes through draftsApi,
which sends the research bearer, and shows "Research user not set" on a 401
rather than an empty queue.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.auth.bearer import token_to_owner_map

_GATED = ("approve", "dismiss")


@pytest.fixture(autouse=True)
def _health_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bifrost_research.api.health.run_startup_schema_guard",
        lambda: None,
    )
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


@pytest.fixture
def auth_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RESEARCH_USERS", "alice:tok_alice")
    monkeypatch.delenv("RESEARCH_API_TOKEN", raising=False)
    token_to_owner_map.cache_clear()
    yield
    token_to_owner_map.cache_clear()


class _Env:
    def __init__(self) -> None:
        self.connects = 0
        self.draft: dict[str, Any] = {
            "id": "aid_1",
            "kind": "morning_brief",
            "status": "pending",
            "payload": {"title": "Brief"},
            "linked_action_id": "aal_1",
        }
        self.status_updates: list[dict[str, Any]] = []
        self.action_updates: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()

    def _connect() -> MagicMock:
        e.connects += 1
        return MagicMock()

    monkeypatch.setattr("bifrost_research.api.agents.connect", _connect)
    monkeypatch.setattr(
        "bifrost_research.api.agents.draft_repo.get_draft",
        lambda conn, draft_id: e.draft,
    )

    def _update_draft(conn, draft_id, *, status):
        e.status_updates.append({"draft_id": draft_id, "status": status})
        return {**e.draft, "status": status}

    monkeypatch.setattr(
        "bifrost_research.api.agents.draft_repo.update_draft_status", _update_draft
    )

    def _update_action(conn, action_id, **k):
        e.action_updates.append({"id": action_id, **k})
        return {"id": action_id, **k}

    monkeypatch.setattr(
        "bifrost_research.api.agents.action_repo.update_action_status", _update_action
    )
    def _list(conn, **k):
        e.list_calls.append(k)
        return [e.draft]

    monkeypatch.setattr("bifrost_research.api.agents.draft_repo.list_drafts", _list)
    monkeypatch.setattr("bifrost_research.api.agents.draft_repo.count_pending", lambda conn: 1)
    monkeypatch.setattr(
        "bifrost_research.api.agents.action_repo.insert_action",
        lambda conn, **k: {"id": "aal_new", **k},
    )
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.mark.parametrize("verb", _GATED)
def test_anonymous_decision_is_refused(
    client: TestClient, env: _Env, auth_on: None, verb: str
) -> None:
    resp = client.post(f"/research/drafts/aid_1/{verb}")
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    assert env.connects == 0
    assert env.status_updates == []


@pytest.mark.parametrize("verb", _GATED)
def test_unknown_token_is_refused(
    client: TestClient, env: _Env, auth_on: None, verb: str
) -> None:
    resp = client.post(
        f"/research/drafts/aid_1/{verb}",
        headers={"Authorization": "Bearer tok_mallory"},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    assert env.connects == 0
    assert env.status_updates == []


def test_research_user_can_dismiss(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/drafts/aid_1/dismiss",
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["draft"]["status"] == "dismissed"
    assert env.status_updates == [{"draft_id": "aid_1", "status": "dismissed"}]
    # No body: the resolved user signs the rejection, as approve does.
    assert env.action_updates[0]["approved_by"] == "alice"
    assert env.action_updates[0]["status"] == "rejected"


def test_dismiss_keeps_body_approved_by(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/drafts/aid_1/dismiss",
        json={"approved_by": "owner"},
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert env.action_updates[0]["approved_by"] == "owner"


def test_anonymous_list_is_refused(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.get("/research/drafts")
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    assert env.connects == 0
    assert env.list_calls == []


def test_unknown_token_list_is_refused(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.get("/research/drafts", headers={"Authorization": "Bearer tok_mallory"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    assert env.connects == 0
    assert env.list_calls == []


def test_research_user_can_list(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.get(
        "/research/drafts?kind=morning_brief",
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["count"] == 1 and data["pending_count"] == 1
    assert env.list_calls[0]["kind"] == "morning_brief"
