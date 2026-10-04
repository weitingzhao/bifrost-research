"""Batch-run and rate need a research user.

Both are reached from the console with the research bearer already, and nothing
else calls them over HTTP (the scheduled harness and the MCP tool call the Python
directly). A batch run spends model budget and can auto-approve under Trust L0; a
rating writes the run's outputs and the draft's rows. The objective and run writes
the frontend still sends without the bearer are not gated yet.
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
    "/research/objectives/obj_1/batch-run",
    "/research/objective-runs/run_1/rate",
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
        self.batches: list[dict[str, Any]] = []
        self.rated: list[str] = []


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()

    def _connect() -> MagicMock:
        e.connects += 1
        return MagicMock()

    def _start(conn: Any, obj: dict[str, Any], *, curate_after: bool, overrides: Any) -> dict:
        e.batches.append({"objective_id": obj["id"], "curate_after": curate_after})
        return {"run": {"id": "run_new", "status": "running"}, "started": True}

    def _rate(conn: Any, run_id: str) -> dict[str, Any]:
        e.rated.append(run_id)
        return {"run_id": run_id, "decision": "keep"}

    monkeypatch.setattr("bifrost_research.api.harness.connect", _connect)
    monkeypatch.setattr(
        "bifrost_research.api.harness.obj_repo.get_objective",
        lambda conn, oid: {"id": oid, "status": "active", "policy_json": {}},
    )
    monkeypatch.setattr(
        "bifrost_research.copilot.harness.batch_orchestrate.start_async_batch", _start
    )
    monkeypatch.setattr("bifrost_research.copilot.harness.rating.rate_run", _rate)
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.mark.parametrize("path", _ROUTES)
def test_anonymous_call_is_refused(
    client: TestClient, env: _Env, auth_on: None, path: str
) -> None:
    resp = client.post(path)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    assert env.connects == 0
    assert env.batches == [] and env.rated == []


@pytest.mark.parametrize("path", _ROUTES)
def test_unknown_token_is_refused(
    client: TestClient, env: _Env, auth_on: None, path: str
) -> None:
    resp = client.post(path, headers={"Authorization": "Bearer tok_mallory"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    assert env.connects == 0
    assert env.batches == [] and env.rated == []


def test_research_user_can_batch_run(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/objectives/obj_1/batch-run",
        json={"curate_after": False},
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["run"]["id"] == "run_new"
    assert env.batches == [{"objective_id": "obj_1", "curate_after": False}]


def test_research_user_can_rate(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/objective-runs/run_1/rate",
        headers={"Authorization": "Bearer tok_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["run_id"] == "run_1"
    assert env.rated == ["run_1"]
