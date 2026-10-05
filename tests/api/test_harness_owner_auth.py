"""Objective and run writes need a research user.

Every write here is reached from the console with the research bearer, and
nothing else calls them over HTTP (the scheduled harness and the MCP tool call
the Python directly). A run or batch run spends model budget and can
auto-approve under Trust L0; curate calls a model; a rating writes the run's
outputs and the draft's rows; create / patch / delete change the objective list,
and a forced run delete takes its candidates and pending drafts with it.

The objective's ``owner_id`` used to default to the literal ``"owner"``; a body
that does not name one is now owned by the resolved research user.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.auth.bearer import token_to_owner_map

_GATED = (
    ("POST", "/research/objectives", {"title": "T", "description": "D"}),
    ("PATCH", "/research/objectives/obj_1", {"status": "archived"}),
    ("DELETE", "/research/objectives/obj_1", None),
    ("POST", "/research/objectives/obj_1/run", None),
    ("POST", "/research/objectives/obj_1/batch-run", None),
    ("DELETE", "/research/objective-runs/run_1", None),
    ("POST", "/research/objective-runs/run_1/curate", None),
    ("POST", "/research/objective-runs/run_1/rate", None),
)

_ALICE = {"Authorization": "Bearer tok_alice"}


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
        self.writes: list[tuple[str, Any]] = []
        self.created: list[dict[str, Any]] = []


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()

    def _connect() -> MagicMock:
        e.connects += 1
        return MagicMock()

    def _create(conn: Any, **kw: Any) -> dict[str, Any]:
        e.created.append(kw)
        return {"id": "obj_new", **kw}

    def _set_status(conn: Any, oid: str, *, status: str) -> dict[str, Any]:
        e.writes.append(("status", oid))
        return {"id": oid, "status": status}

    def _delete_objective(conn: Any, oid: str) -> bool:
        e.writes.append(("delete_objective", oid))
        return True

    def _delete_run(conn: Any, run_id: str) -> bool:
        e.writes.append(("delete_run", run_id))
        return True

    def _run(conn: Any, *, objective: dict[str, Any]) -> dict[str, Any]:
        e.writes.append(("run", objective["id"]))
        return {"run": {"id": "run_new", "status": "completed"}}

    def _start(conn: Any, obj: dict[str, Any], *, curate_after: bool, overrides: Any) -> dict:
        e.writes.append(("batch", {"objective_id": obj["id"], "curate_after": curate_after}))
        return {"run": {"id": "run_new", "status": "running"}, "started": True}

    def _curate(conn: Any, run_id: str) -> dict[str, Any]:
        e.writes.append(("curate", run_id))
        return {"run_id": run_id, "drafts": []}

    def _rate(conn: Any, run_id: str) -> dict[str, Any]:
        e.writes.append(("rate", run_id))
        return {"run_id": run_id, "decision": "keep"}

    mod = "bifrost_research.api.harness"
    monkeypatch.setattr(f"{mod}.connect", _connect)
    monkeypatch.setattr(f"{mod}.obj_repo.create_objective", _create)
    monkeypatch.setattr(
        f"{mod}.obj_repo.get_objective",
        lambda conn, oid: {"id": oid, "status": "active", "policy_json": {}},
    )
    monkeypatch.setattr(f"{mod}.obj_repo.set_objective_status", _set_status)
    monkeypatch.setattr(f"{mod}.obj_repo.count_runs", lambda conn, oid: 0)
    monkeypatch.setattr(f"{mod}.obj_repo.delete_objective", _delete_objective)
    monkeypatch.setattr(
        f"{mod}.obj_repo.get_run",
        lambda conn, rid: {"id": rid, "status": "awaiting_approval"},
    )
    monkeypatch.setattr(f"{mod}.obj_repo.count_candidates_for_run", lambda conn, rid: 0)
    monkeypatch.setattr(f"{mod}.obj_repo.delete_run", _delete_run)
    monkeypatch.setattr("bifrost_research.copilot.harness.runtime.run_objective", _run)
    monkeypatch.setattr(
        "bifrost_research.copilot.harness.batch_orchestrate.start_async_batch", _start
    )
    monkeypatch.setattr("bifrost_research.copilot.curator.runtime.run_curator_for_run", _curate)
    monkeypatch.setattr("bifrost_research.copilot.harness.rating.rate_run", _rate)
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.mark.parametrize(("method", "path", "body"), _GATED)
def test_anonymous_call_is_refused(
    client: TestClient, env: _Env, auth_on: None, method: str, path: str, body: Any
) -> None:
    resp = client.request(method, path, json=body)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    assert env.connects == 0
    assert env.writes == [] and env.created == []


@pytest.mark.parametrize(("method", "path", "body"), _GATED)
def test_unknown_token_is_refused(
    client: TestClient, env: _Env, auth_on: None, method: str, path: str, body: Any
) -> None:
    resp = client.request(method, path, json=body, headers={"Authorization": "Bearer tok_mallory"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    assert env.connects == 0
    assert env.writes == [] and env.created == []


def test_research_user_creates_and_owns_the_objective(
    client: TestClient, env: _Env, auth_on: None
) -> None:
    resp = client.post(
        "/research/objectives",
        json={"title": "T", "description": "D"},
        headers=_ALICE,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["id"] == "obj_new"
    # No owner_id in the body: the resolved user owns it, not "owner".
    assert env.created[0]["owner_id"] == "alice"


def test_create_keeps_body_owner_id(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/objectives",
        json={"title": "T", "description": "D", "owner_id": "owner"},
        headers=_ALICE,
    )
    assert resp.status_code == 200, resp.text
    assert env.created[0]["owner_id"] == "owner"


def test_research_user_can_patch(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.patch(
        "/research/objectives/obj_1", json={"status": "archived"}, headers=_ALICE
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "archived"
    assert env.writes == [("status", "obj_1")]


def test_research_user_can_delete_objective(
    client: TestClient, env: _Env, auth_on: None
) -> None:
    resp = client.delete("/research/objectives/obj_1", headers=_ALICE)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"id": "obj_1", "deleted": True}
    assert env.writes == [("delete_objective", "obj_1")]


def test_research_user_can_run(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post("/research/objectives/obj_1/run", headers=_ALICE)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["run"]["id"] == "run_new"
    assert env.writes == [("run", "obj_1")]


def test_research_user_can_batch_run(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(
        "/research/objectives/obj_1/batch-run",
        json={"curate_after": False},
        headers=_ALICE,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["run"]["id"] == "run_new"
    assert env.writes == [("batch", {"objective_id": "obj_1", "curate_after": False})]


def test_research_user_can_delete_run(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.delete("/research/objective-runs/run_1", headers=_ALICE)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["deleted"] is True
    assert env.writes == [("delete_run", "run_1")]


def test_research_user_can_curate(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post("/research/objective-runs/run_1/curate", headers=_ALICE)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["run_id"] == "run_1"
    assert env.writes == [("curate", "run_1")]


def test_research_user_can_rate(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post("/research/objective-runs/run_1/rate", headers=_ALICE)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["run_id"] == "run_1"
    assert env.writes == [("rate", "run_1")]
