"""Retiring a hypothesis needs a research user.

Retire takes a hypothesis off the board and nothing un-retires it over HTTP, so
an anonymous caller must not reach it. The other hypothesis writes are not gated
yet: the frontend sends them without the research bearer.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.auth.bearer import token_to_owner_map


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
        self.retired: list[str] = []


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()

    def _connect() -> MagicMock:
        e.connects += 1
        return MagicMock()

    def _retire(conn: Any, hypothesis_id: str) -> dict[str, Any]:
        e.retired.append(hypothesis_id)
        return {"id": hypothesis_id, "status": "retired"}

    monkeypatch.setattr("bifrost_research.api.hypothesis.connect", _connect)
    monkeypatch.setattr("bifrost_research.api.hypothesis.repo.retire_hypothesis", _retire)
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_anonymous_retire_is_refused(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post("/research/hypothesis/hyp_1/retire")
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    assert env.connects == 0
    assert env.retired == []


def test_unknown_token_retire_is_refused(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/hypothesis/hyp_1/retire",
        headers={"Authorization": "Bearer tok_mallory"},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    assert env.connects == 0
    assert env.retired == []


def test_research_user_can_retire(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/hypothesis/hyp_1/retire",
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "retired"
    assert env.retired == ["hyp_1"]
