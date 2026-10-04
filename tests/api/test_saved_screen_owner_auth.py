"""Editing or retiring a saved screen needs a research user.

Trade's result face renders a saved screen read-only by id, so a PATCH changes
what that face shows and a retire takes it away. Nothing calls either over HTTP
today. Create is not gated yet: the frontend saves screens without the research
bearer.
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
        self.writes: list[tuple[str, str]] = []


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()

    def _connect() -> MagicMock:
        e.connects += 1
        return MagicMock()

    def _patch(conn: Any, screen_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        e.writes.append(("patch", screen_id))
        return {"id": screen_id, **updates}

    def _retire(conn: Any, screen_id: str) -> dict[str, Any]:
        e.writes.append(("retire", screen_id))
        return {"id": screen_id, "is_active": False}

    monkeypatch.setattr("bifrost_research.api.saved_screen.connect", _connect)
    monkeypatch.setattr("bifrost_research.api.saved_screen.repo.patch_screen", _patch)
    monkeypatch.setattr("bifrost_research.api.saved_screen.repo.retire_screen", _retire)
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


_CALLS = (
    ("PATCH", "/research/screens/scr_1", {"name": "Renamed"}),
    ("POST", "/research/screens/scr_1/retire", None),
)


@pytest.mark.parametrize(("method", "path", "body"), _CALLS)
def test_anonymous_write_is_refused(
    client: TestClient, env: _Env, auth_on: None, method: str, path: str, body: Any
) -> None:
    resp = client.request(method, path, json=body)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    assert env.connects == 0
    assert env.writes == []


@pytest.mark.parametrize(("method", "path", "body"), _CALLS)
def test_unknown_token_is_refused(
    client: TestClient, env: _Env, auth_on: None, method: str, path: str, body: Any
) -> None:
    resp = client.request(
        method, path, json=body, headers={"Authorization": "Bearer tok_mallory"}
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    assert env.connects == 0
    assert env.writes == []


def test_research_user_can_patch(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.patch(
        "/research/screens/scr_1",
        json={"name": "Renamed"},
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["name"] == "Renamed"
    assert env.writes == [("patch", "scr_1")]


def test_research_user_can_retire(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/screens/scr_1/retire",
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert env.writes == [("retire", "scr_1")]
