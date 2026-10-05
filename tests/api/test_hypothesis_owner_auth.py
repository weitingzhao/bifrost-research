"""Hypothesis writes need a research user.

Retire takes a hypothesis off the board and nothing un-retires it over HTTP;
create and patch write the board itself; refresh-trajectory simulates the
trajectory and merges its summary into the hypothesis. The frontend sends all
four with the research bearer (hypothesisApi, refreshHypothesisTrajectory), and
nothing else calls them over HTTP. Reads are gated too (0.167.0).
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


_GATED = (
    ("POST", "/research/hypothesis", {"title": "T", "thesis": "because"}),
    ("PATCH", "/research/hypothesis/hyp_1", {"title": "T2"}),
    ("POST", "/research/hypothesis/hyp_1/retire", None),
    ("POST", "/research/hypothesis/hyp_1/refresh-trajectory", None),
)


class _Env:
    def __init__(self) -> None:
        self.connects = 0
        self.retired: list[str] = []
        self.created: list[dict[str, Any]] = []
        self.patched: list[tuple[str, dict[str, Any]]] = []
        self.simulated: list[str] = []

    def wrote(self) -> bool:
        return bool(self.retired or self.created or self.patched or self.simulated)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()

    def _connect() -> MagicMock:
        e.connects += 1
        return MagicMock()

    def _retire(conn: Any, hypothesis_id: str) -> dict[str, Any]:
        e.retired.append(hypothesis_id)
        return {"id": hypothesis_id, "status": "retired"}

    def _create(conn: Any, **kw: Any) -> dict[str, Any]:
        e.created.append(kw)
        return {"id": "hyp_new", **kw}

    def _patch(conn: Any, hypothesis_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        e.patched.append((hypothesis_id, updates))
        return {"id": hypothesis_id, **updates}

    def _trajectory(conn: Any, *, symbol: str, entry_date: Any, structure: str) -> tuple:
        # Stands in for simulate_entry, which reads option marks from the DB.
        e.simulated.append(symbol)
        return entry_date, [{"pnl_since_entry": 1.0, "final_pnl": None}]

    mod = "bifrost_research.api.hypothesis"
    monkeypatch.setattr(f"{mod}.connect", _connect)
    monkeypatch.setattr(f"{mod}.repo.retire_hypothesis", _retire)
    monkeypatch.setattr(f"{mod}.repo.create_hypothesis", _create)
    monkeypatch.setattr(f"{mod}.repo.patch_hypothesis", _patch)
    monkeypatch.setattr(
        f"{mod}.repo.get_hypothesis",
        lambda conn, hid: {
            "id": hid,
            "symbols": ["NVDA"],
            "created_at": "2026-09-01T14:00:00Z",
            "origin_ref": {},
        },
    )
    monkeypatch.setattr(f"{mod}._fetch_trajectory_rows", _trajectory)
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.mark.parametrize(("method", "path", "body"), _GATED)
def test_anonymous_write_is_refused(
    client: TestClient, env: _Env, auth_on: None, method: str, path: str, body: Any
) -> None:
    resp = client.request(method, path, json=body)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    assert env.connects == 0
    assert not env.wrote()


@pytest.mark.parametrize(("method", "path", "body"), _GATED)
def test_unknown_token_write_is_refused(
    client: TestClient, env: _Env, auth_on: None, method: str, path: str, body: Any
) -> None:
    resp = client.request(
        method, path, json=body, headers={"Authorization": "Bearer tok_mallory"}
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    assert env.connects == 0
    assert not env.wrote()


def test_research_user_can_create(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/hypothesis",
        json={"title": "T", "thesis": "because", "symbols": ["NVDA"]},
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["id"] == "hyp_new"
    assert env.created[0]["title"] == "T"


def test_research_user_can_patch(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.patch(
        "/research/hypothesis/hyp_1",
        json={"title": "T2"},
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert env.patched == [("hyp_1", {"title": "T2"})]


def test_research_user_can_refresh_trajectory(
    client: TestClient, env: _Env, auth_on: None
) -> None:
    resp = client.post(
        "/research/hypothesis/hyp_1/refresh-trajectory",
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["count"] == 1
    assert env.simulated == ["NVDA"]
    assert env.patched[0][0] == "hyp_1"
    assert "trajectory_summary" in env.patched[0][1]["origin_ref"]


def test_research_user_can_retire(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/hypothesis/hyp_1/retire",
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "retired"
    assert env.retired == ["hyp_1"]
