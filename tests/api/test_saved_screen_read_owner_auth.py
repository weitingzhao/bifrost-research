"""Saved screen reads need a research user.

The frontend's screensApi (fetchSavedScreens) sends the research bearer and its
readers show "not signed in" on a 401. Nothing calls GET by id over HTTP; it is
gated with the list.
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
        self.reads: list[str] = []

    def connect(self) -> MagicMock:
        self.connects += 1
        return MagicMock()

    def read(self, name: str, value: Any) -> Any:
        self.reads.append(name)
        return value


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


_READS = (
    "/research/screens",
    "/research/screens/scr_1",
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()
    mod = "bifrost_research.api.saved_screen"
    monkeypatch.setattr(f"{mod}.connect", e.connect)
    monkeypatch.setattr(
        f"{mod}.repo.list_screens", lambda conn, *, include_retired: e.read("list", [])
    )
    monkeypatch.setattr(f"{mod}.repo.get_screen", lambda conn, sid: e.read("one", {"id": sid}))
    return e


@pytest.mark.parametrize("path", _READS)
def test_anonymous_read_is_refused(client: TestClient, env: _Env, auth_on: None, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    assert env.connects == 0
    assert env.reads == []


@pytest.mark.parametrize("path", _READS)
def test_unknown_token_read_is_refused(
    client: TestClient, env: _Env, auth_on: None, path: str
) -> None:
    resp = client.get(path, headers={"Authorization": "Bearer tok_mallory"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    assert env.connects == 0
    assert env.reads == []


@pytest.mark.parametrize("path", _READS)
def test_research_user_can_read(client: TestClient, env: _Env, auth_on: None, path: str) -> None:
    resp = client.get(path, headers={"Authorization": "Bearer tok_alice"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert env.reads
