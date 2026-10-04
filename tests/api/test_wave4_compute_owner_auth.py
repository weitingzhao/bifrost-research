"""The wave-4 compute triggers need a research user.

Terrain, forecast sessions (which may call a model), the event-radar pipeline
and settlement are computed on the caller's request. No client calls them over
HTTP — the scheduler runs the same engines in-process — so they are gated before
anyone starts. The engines are replaced here: the gate is the subject, not the
arithmetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.auth.bearer import token_to_owner_map

_TERRAIN = {"symbol": "SPY", "trade_date": "2026-10-02", "spot": 500.0}
_EVENTS = {"payload": "- sample headline"}
_SETTLE = {
    "session_id": "fs_1",
    "symbol": "SPY",
    "trade_date": "2026-10-02",
    "expected_close": 500.0,
    "actual_close": 501.0,
}
_AGG = {"settlements": [{"session_id": "fs_1", "symbol": "SPY"}]}

_CALLS = (
    ("/research/forecast/terrain/compute", _TERRAIN),
    ("/research/forecast/sessions/compute", {**_TERRAIN, "enrich": True}),
    ("/research/event-radar/run", _EVENTS),
    ("/research/events/ingest", _EVENTS),
    ("/research/backtest/settle", _SETTLE),
    ("/research/backtest/aggregate", _AGG),
    ("/research/forecast/settle", _SETTLE),
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


def _result(tag: str) -> MagicMock:
    out = MagicMock()
    out.to_dict.return_value = {"engine": tag}
    return out


@pytest.fixture
def engines(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace each engine the routes call (and the DB / model handles); record calls."""
    called: list[str] = []

    def _rec(tag: str):
        def _fn(*_a: Any, **_k: Any) -> MagicMock:
            called.append(tag)
            return _result(tag)

        return _fn

    def _no_db() -> None:
        called.append("connect")
        raise AssertionError("compute routes must not open a connection")

    mod = "bifrost_research.api.wave4"
    monkeypatch.setattr(f"{mod}.connect", _no_db)
    monkeypatch.setattr(f"{mod}.compute_market_terrain", _rec("terrain"))
    monkeypatch.setattr(f"{mod}.build_forecast_session", _rec("session"))
    monkeypatch.setattr(f"{mod}.get_default_provider", _rec("llm"))
    monkeypatch.setattr(f"{mod}.run_pipeline", _rec("event_radar"))
    monkeypatch.setattr(f"{mod}.settle_forecast", _rec("settle"))
    monkeypatch.setattr(f"{mod}.aggregate_accuracy", _rec("aggregate"))
    return called


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.mark.parametrize(("path", "body"), _CALLS)
def test_anonymous_compute_is_refused(
    client: TestClient, engines: list[str], auth_on: None, path: str, body: Any
) -> None:
    resp = client.post(path, json=body)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "research authorization required"
    assert engines == []


@pytest.mark.parametrize(("path", "body"), _CALLS)
def test_unknown_token_is_refused(
    client: TestClient, engines: list[str], auth_on: None, path: str, body: Any
) -> None:
    resp = client.post(path, json=body, headers={"Authorization": "Bearer tok_mallory"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid research token"
    assert engines == []


@pytest.mark.parametrize(("path", "body"), _CALLS)
def test_research_user_can_compute(
    client: TestClient, engines: list[str], auth_on: None, path: str, body: Any
) -> None:
    resp = client.post(path, json=body, headers={"Authorization": "Bearer tok_alice"})
    assert resp.status_code == 200, resp.text
    assert engines, "the route should have reached its engine"
    assert "connect" not in engines
