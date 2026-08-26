"""Event-driven backtest HTTP tests — Wave RS-C4.

Mocks the engine + repo layers so tests run without a live DB.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api import backtest_event as backtest_api
from bifrost_research.api.app import create_app


class _FakeConnection:
    """Minimal fake — the router closes it in ``finally``."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(backtest_api, "connect", lambda: _FakeConnection())
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------


def test_backtest_event_routes_registered() -> None:
    client = TestClient(create_app())
    paths = set(client.app.openapi()["paths"])
    assert "/research/backtest/event-query" in paths
    assert "/research/backtest/runs" in paths
    assert "/research/backtest/run/{run_id}" in paths


# ---------------------------------------------------------------------------
# POST /research/backtest/event-query
# ---------------------------------------------------------------------------


def _default_engine_result() -> dict[str, Any]:
    return {
        "runs": [
            {
                "event_date": "2026-06-01",
                "symbol": "NVDA",
                "entry_ts": "2026-05-31",
                "exit_ts": "2026-06-02",
                "pnl": 1.5,
                "mfe": 0.05,
                "mae": -0.02,
                "legs": [],
                "notes": "D10 BLOCKED",
            }
        ],
        "summary": {
            "n_events": 1,
            "win_rate": 1.0,
            "avg_pnl": 1.5,
            "median_pnl": 1.5,
            "sharpe_annual": 0.5,
            "max_drawdown": 0.0,
            "avg_mfe": 0.05,
            "avg_mae": -0.02,
        },
        "event_source": "stub",
        "event_source_notes": "earnings source: stub",
        "skipped_events": 0,
        "template": "long_atm_straddle",
        "template_kwargs": {},
        "event_def": {"kind": "earnings", "params": {"symbols": ["NVDA"]}},
        "lookback_years": 3,
        "advisory": "D10 BLOCKED",
    }


def test_event_query_happy_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine_calls: list[Any] = []
    repo_creates: list[dict[str, Any]] = []

    def fake_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        engine_calls.append((args, kwargs))
        return _default_engine_result()

    def fake_create_run(conn: Any, **kwargs: Any) -> dict[str, Any]:
        repo_creates.append(kwargs)
        return {
            "id": "bt-1234abcd",
            "hypothesis_id": kwargs.get("hypothesis_id"),
            "event_def": kwargs["event_def"],
            "strategy_template": kwargs["strategy_template"],
            "fill_config": kwargs["fill_config"],
            "lookback_years": kwargs["lookback_years"],
            "summary": kwargs["summary"],
            "walk_forward": kwargs.get("walk_forward"),
            "benchmark": kwargs.get("benchmark"),
            "created_at": "2026-06-01T00:00:00+00:00",
        }

    monkeypatch.setattr(backtest_api, "run_event_query", fake_run)
    monkeypatch.setattr(backtest_api.repo, "create_run", fake_create_run)
    monkeypatch.setattr(
        backtest_api.repo, "append_to_hypothesis", lambda *a, **kw: True
    )

    resp = client.post(
        "/research/backtest/event-query",
        json={
            "event_def": {"kind": "earnings", "params": {"symbols": ["NVDA"]}},
            "strategy_template": "long_atm_straddle",
            "lookback_years": 3,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["run_id"] == "bt-1234abcd"
    assert data["summary"]["n_events"] == 1
    assert data["summary"]["avg_pnl"] == 1.5
    assert data["event_source"] == "stub"
    assert data["walk_forward"] is None
    assert data["benchmark"] is None
    assert len(engine_calls) == 1
    assert len(repo_creates) == 1
    # Envelope + advisory
    assert "D10" in data["advisory"]


def test_event_query_rejects_unknown_template(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = client.post(
        "/research/backtest/event-query",
        json={
            "event_def": {"kind": "earnings", "params": {}},
            "strategy_template": "not_a_real_strategy",
            "lookback_years": 3,
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "unknown strategy_template" in body["detail"]


def test_event_query_maps_notimplemented_to_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_nie(*args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("sql not implemented in v1")

    monkeypatch.setattr(backtest_api, "run_event_query", raise_nie)
    resp = client.post(
        "/research/backtest/event-query",
        json={
            "event_def": {"kind": "sql", "params": {"query": "SELECT 1"}},
            "strategy_template": "long_atm_call",
            "lookback_years": 3,
        },
    )
    assert resp.status_code == 400


def test_event_query_hypothesis_link_calls_append(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    append_calls: list[Any] = []

    def fake_run(*a: Any, **kw: Any) -> dict[str, Any]:
        return _default_engine_result()

    def fake_create_run(conn: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "id": "bt-linked-42",
            "hypothesis_id": kwargs.get("hypothesis_id"),
            "summary": kwargs["summary"],
            "event_def": kwargs["event_def"],
            "strategy_template": kwargs["strategy_template"],
            "fill_config": kwargs["fill_config"],
            "lookback_years": kwargs["lookback_years"],
            "walk_forward": None,
            "benchmark": None,
            "created_at": "2026-06-01T00:00:00+00:00",
        }

    def fake_append(conn: Any, hypothesis_id: str, run_id: str) -> bool:
        append_calls.append((hypothesis_id, run_id))
        return True

    monkeypatch.setattr(backtest_api, "run_event_query", fake_run)
    monkeypatch.setattr(backtest_api.repo, "create_run", fake_create_run)
    monkeypatch.setattr(backtest_api.repo, "append_to_hypothesis", fake_append)

    resp = client.post(
        "/research/backtest/event-query",
        json={
            "event_def": {"kind": "earnings", "params": {"symbols": ["NVDA"]}},
            "strategy_template": "long_atm_straddle",
            "lookback_years": 3,
            "hypothesis_id": "h-abc",
        },
    )
    assert resp.status_code == 200
    assert append_calls == [("h-abc", "bt-linked-42")]


def test_event_query_no_hypothesis_id_sets_null(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backtest_api, "run_event_query", lambda *a, **kw: _default_engine_result())

    captured: dict[str, Any] = {}

    def fake_create_run(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "bt-x", "hypothesis_id": kwargs.get("hypothesis_id"), "summary": kwargs["summary"]}

    monkeypatch.setattr(backtest_api.repo, "create_run", fake_create_run)
    monkeypatch.setattr(backtest_api.repo, "append_to_hypothesis", lambda *a, **kw: True)

    resp = client.post(
        "/research/backtest/event-query",
        json={
            "event_def": {"kind": "opex", "params": {}},
            "strategy_template": "long_atm_call",
            "lookback_years": 2,
        },
    )
    assert resp.status_code == 200
    assert captured["hypothesis_id"] is None


def test_event_query_include_walk_forward_and_benchmark(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backtest_api, "run_event_query", lambda *a, **kw: _default_engine_result())
    monkeypatch.setattr(backtest_api.repo, "create_run", lambda *a, **kw: {"id": "bt-wf", "summary": kw["summary"]})
    monkeypatch.setattr(backtest_api.repo, "append_to_hypothesis", lambda *a, **kw: True)

    resp = client.post(
        "/research/backtest/event-query",
        json={
            "event_def": {"kind": "earnings", "params": {"symbols": ["NVDA"]}},
            "strategy_template": "long_atm_straddle",
            "lookback_years": 3,
            "include_walk_forward": True,
            "include_benchmark": True,
        },
    )
    assert resp.status_code == 200
    # With only one run the proxy series is a single point → windows may be
    # empty; contract is that the payload keys exist regardless.
    data = resp.json()["data"]
    assert "walk_forward" in data
    assert "benchmark" in data


# ---------------------------------------------------------------------------
# GET /research/backtest/runs + /run/{id}
# ---------------------------------------------------------------------------


def test_list_runs_filter_by_hypothesis(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_list(conn: Any, **kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs["hypothesis_id"] == "h-1"
        return [
            {
                "id": "bt-a",
                "hypothesis_id": "h-1",
                "summary": {},
                "created_at": "2026-06-01T00:00:00+00:00",
            }
        ]

    monkeypatch.setattr(backtest_api.repo, "list_runs", fake_list)
    resp = client.get("/research/backtest/runs", params={"hypothesis_id": "h-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["count"] == 1
    assert body["data"]["rows"][0]["hypothesis_id"] == "h-1"


def test_get_run_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backtest_api.repo, "get_run", lambda conn, rid: None)
    resp = client.get("/research/backtest/run/does-not-exist")
    assert resp.status_code == 404


def test_get_run_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backtest_api.repo,
        "get_run",
        lambda conn, rid: {
            "id": rid,
            "hypothesis_id": None,
            "summary": {"n_events": 1},
            "created_at": "2026-06-01T00:00:00+00:00",
        },
    )
    resp = client.get("/research/backtest/run/bt-42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["row"]["id"] == "bt-42"
