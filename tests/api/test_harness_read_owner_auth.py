"""Objective, run and Loop standing reads need a research user.

The objective list, a run's cost estimate, the autopilot standing, the Trust
gate and the runs (list and one) are read by the console with the research
bearer (fetchObjectives, fetchRunEstimate, fetchAutopilotStanding,
fetchLoopTrust, fetchObjectiveRuns, fetchObjectiveRun*), each of which shows
"not signed in" on a 401. The scheduled harness and the MCP tools call the
Python directly, and no other service reads these over HTTP.
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
    "/research/objectives",
    "/research/objectives/obj_1/run-estimate",
    "/research/loop/autopilot",
    "/research/loop/trust",
    "/research/objective-runs/run_1",
    "/research/objective-runs",
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()
    mod = "bifrost_research.api.harness"
    harness = "bifrost_research.copilot.harness"
    monkeypatch.setattr(f"{mod}.connect", e.connect)
    monkeypatch.setattr(
        f"{mod}.obj_repo.list_objectives", lambda conn, **kw: e.read("objectives", [])
    )
    monkeypatch.setattr(
        f"{mod}.obj_repo.get_objective",
        lambda conn, oid: e.read("objective", {"id": oid, "policy_json": {}}),
    )
    monkeypatch.setattr(f"{mod}.obj_repo.list_runs", lambda conn, **kw: e.read("runs", []))
    monkeypatch.setattr(
        f"{mod}.obj_repo.get_run",
        lambda conn, rid: e.read("run", {"id": rid, "objective_id": "obj_1"}),
    )
    monkeypatch.setattr(f"{harness}.persona_judge.eval_models", lambda chosen=None: [])
    monkeypatch.setattr(
        f"{harness}.run_estimate.estimate_run", lambda **kw: e.read("estimate", {"usd": 0.0})
    )
    monkeypatch.setattr(f"{harness}.run_estimate.estimate_summary", lambda est: "no history")
    monkeypatch.setattr(
        f"{harness}.standing.autopilot_standing", lambda conn: e.read("standing", {})
    )
    monkeypatch.setattr(
        f"{harness}.batch_orchestrate.trust_status", lambda: e.read("trust", {"l0": False})
    )
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
