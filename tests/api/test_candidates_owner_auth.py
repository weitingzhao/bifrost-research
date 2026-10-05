"""Candidate pool writes need a research user.

Adding candidates puts names in front of the Loop, promoting one writes a
hypothesis, dismissing one closes it — none of that may come from an anonymous
caller. The frontend's addCandidates / promoteCandidate / dismissCandidate send
the research bearer, and nothing else calls these over HTTP.

The batch's ``owner_id`` used to default to the literal ``"owner"``; a body that
does not name one is now signed by the resolved research user.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.auth.bearer import token_to_owner_map

_ROUTES = (
    ("/research/candidates", {"items": [{"symbol": "NVDA"}]}),
    ("/research/candidates/cand_1/promote", {}),
    ("/research/candidates/cand_1/dismiss", {}),
)


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
        self.created: list[dict[str, Any]] = []
        self.promoted: list[str] = []
        self.dismissed: list[str] = []
        self.hypotheses: list[dict[str, Any]] = []


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()

    def _connect() -> MagicMock:
        e.connects += 1
        return MagicMock()

    def _create(conn: Any, **kw: Any) -> dict[str, Any]:
        e.created.append(kw)
        return {"id": f"cand_{len(e.created)}", **kw}

    def _promote(conn: Any, candidate_id: str, *, hypothesis_id: str) -> dict[str, Any]:
        e.promoted.append(candidate_id)
        return {"id": candidate_id, "status": "promoted", "hypothesis_id": hypothesis_id}

    def _dismiss(conn: Any, candidate_id: str) -> dict[str, Any]:
        e.dismissed.append(candidate_id)
        return {"id": candidate_id, "status": "dismissed"}

    def _create_hyp(conn: Any, **kw: Any) -> dict[str, Any]:
        e.hypotheses.append(kw)
        return {"id": "hyp_new", **kw}

    mod = "bifrost_research.api.candidates"
    monkeypatch.setattr(f"{mod}.connect", _connect)
    monkeypatch.setattr(f"{mod}.repo.create_candidate", _create)
    monkeypatch.setattr(
        f"{mod}.repo.get_candidate",
        lambda conn, cid: {"id": cid, "symbol": "NVDA", "status": "open", "source": "manual"},
    )
    monkeypatch.setattr(f"{mod}.repo.promote_candidate", _promote)
    monkeypatch.setattr(f"{mod}.repo.dismiss_candidate", _dismiss)
    monkeypatch.setattr(f"{mod}.hyp_repo.create_hypothesis", _create_hyp)
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _no_writes(env: _Env) -> None:
    assert env.connects == 0
    assert env.created == [] and env.promoted == [] and env.dismissed == []
    assert env.hypotheses == []


@pytest.mark.parametrize(("path", "body"), _ROUTES)
def test_anonymous_call_is_refused(
    client: TestClient, env: _Env, auth_on: None, path: str, body: dict[str, Any]
) -> None:
    resp = client.post(path, json=body)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    _no_writes(env)


@pytest.mark.parametrize(("path", "body"), _ROUTES)
def test_unknown_token_is_refused(
    client: TestClient, env: _Env, auth_on: None, path: str, body: dict[str, Any]
) -> None:
    resp = client.post(path, json=body, headers={"Authorization": "Bearer tok_mallory"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    _no_writes(env)


def test_research_user_can_add_and_signs_the_batch(
    client: TestClient, env: _Env, auth_on: None
) -> None:
    resp = client.post(
        "/research/candidates",
        json={"items": [{"symbol": "NVDA"}, {"symbol": "AMD"}]},
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["count"] == 2
    # No owner_id in the body: the resolved user owns the rows, not "owner".
    assert [c["owner_id"] for c in env.created] == ["alice", "alice"]


def test_add_keeps_body_owner_id(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/candidates",
        json={"items": [{"symbol": "NVDA"}], "owner_id": "owner"},
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert env.created[0]["owner_id"] == "owner"


def test_research_user_can_promote(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/candidates/cand_1/promote",
        json={},
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["hypothesis"]["id"] == "hyp_new"
    assert env.promoted == ["cand_1"]


def test_research_user_can_dismiss(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/candidates/cand_1/dismiss",
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "dismissed"
    assert env.dismissed == ["cand_1"]
