"""The event backtest query needs a research user.

It replays years of events on request, writes a ``research.backtest_run`` row
and, given a ``hypothesis_id``, appends that run to the hypothesis. The
frontend's postEventQuery (backtestApi) sends the research bearer, and nothing
else calls it over HTTP. The run reads stay open for now.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.auth.bearer import token_to_owner_map

_PATH = "/research/backtest/event-query"
_BODY = {
    "event_def": {"kind": "earnings", "params": {"symbols": ["NVDA"]}},
    "strategy_template": "long_atm_straddle",
    "hypothesis_id": "hyp_1",
}


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
        self.queries = 0
        self.runs: list[dict[str, Any]] = []
        self.appended: list[tuple[str, str]] = []


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()

    def _connect() -> MagicMock:
        e.connects += 1
        return MagicMock()

    def _query(event_def: Any, **kw: Any) -> dict[str, Any]:
        e.queries += 1
        return {"runs": [], "summary": {"n": 0}, "event_source": "test"}

    def _create_run(conn: Any, **kw: Any) -> dict[str, Any]:
        e.runs.append(kw)
        return {"id": "bt_1", **kw}

    def _append(conn: Any, hypothesis_id: str, run_id: str) -> None:
        e.appended.append((hypothesis_id, run_id))

    mod = "bifrost_research.api.backtest_event"
    monkeypatch.setattr(f"{mod}.connect", _connect)
    monkeypatch.setattr(f"{mod}.run_event_query", _query)
    monkeypatch.setattr(f"{mod}.repo.create_run", _create_run)
    monkeypatch.setattr(f"{mod}.repo.append_to_hypothesis", _append)
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_anonymous_query_is_refused(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(_PATH, json=_BODY)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    assert env.connects == 0 and env.queries == 0
    assert env.runs == [] and env.appended == []


def test_unknown_token_is_refused(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(_PATH, json=_BODY, headers={"Authorization": "Bearer tok_mallory"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    assert env.connects == 0 and env.queries == 0
    assert env.runs == [] and env.appended == []


def test_research_user_can_query(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(_PATH, json=_BODY, headers={"Authorization": "Bearer tok_alice"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["run_id"] == "bt_1"
    assert env.queries == 1
    assert env.appended == [("hyp_1", "bt_1")]
