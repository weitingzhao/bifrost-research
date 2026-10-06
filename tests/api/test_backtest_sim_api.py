"""Option position simulator HTTP routes (P2, 0.170.0).

The engine and the repository are stubbed: the engine has its own tests on a
synthetic chain, and these only check the route — validation, the owner gate,
persistence and its failure before the DDL is applied.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.auth.bearer import token_to_owner_map
from bifrost_research.engines.backtest.sim import SimResult

_PATH = "/research/backtest/sim"
_MOD = "bifrost_research.api.backtest_sim"
_BODY = {"symbols": ["nvda"], "start": "2025-01-02", "end": "2025-12-31", "structure": "put_credit_spread"}


@pytest.fixture(autouse=True)
def _health_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bifrost_research.api.health.run_startup_schema_guard", lambda: None)
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


class _Env:
    def __init__(self) -> None:
        self.sims: list[tuple[list[str], date, date, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.appended: list[tuple[str, str]] = []
        self.create_error: Exception | None = None


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env()

    def _run_sim(conn: Any, symbols: list[str], start: date, end: date, cfg: Any) -> SimResult:
        e.sims.append((symbols, start, end, cfg))
        return SimResult(
            summary={"n_trades": 1, "total_pnl": 42.0},
            trades=[{"seq": 1, "symbol": "NVDA", "pnl": 42.0}],
            equity=[{"as_of": "2025-01-03", "equity": 100042.0}],
            params=cfg.to_dict(),
        )

    def _create(conn: Any, **kw: Any) -> dict[str, Any]:
        if e.create_error is not None:
            raise e.create_error
        e.created.append(kw)
        return {"id": "bt_sim_1", "engine": "sim"}

    monkeypatch.setattr(f"{_MOD}.connect", lambda: MagicMock())
    monkeypatch.setattr(f"{_MOD}.run_sim", _run_sim)
    monkeypatch.setattr(f"{_MOD}.repo.create_sim_run", _create)
    monkeypatch.setattr(f"{_MOD}.repo.append_to_hypothesis", lambda c, h, r: e.appended.append((h, r)))
    return e


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def auth_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RESEARCH_USERS", "alice:tok_alice")
    monkeypatch.delenv("RESEARCH_API_TOKEN", raising=False)
    token_to_owner_map.cache_clear()
    yield
    token_to_owner_map.cache_clear()


def test_routes_registered(client: TestClient) -> None:
    paths = set(client.app.openapi()["paths"])
    assert _PATH in paths
    assert "/research/backtest/sim/structures" in paths
    assert "/research/backtest/sim/{run_id}/detail" in paths


def test_run_is_simulated_and_stored(client: TestClient, env: _Env) -> None:
    resp = client.post(_PATH, json={**_BODY, "hypothesis_id": "hyp_1"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["run_id"] == "bt_sim_1"
    assert data["summary"]["total_pnl"] == 42.0
    symbols, start, end, cfg = env.sims[0]
    assert (start, end) == (date(2025, 1, 2), date(2025, 12, 31))
    assert cfg.structure == "put_credit_spread"
    stored = env.created[0]
    assert stored["params"]["symbols"] == ["NVDA"]
    assert stored["lookback_years"] == 1
    assert env.appended == [("hyp_1", "bt_sim_1")]


def test_persist_false_writes_nothing(client: TestClient, env: _Env) -> None:
    resp = client.post(_PATH, json={**_BODY, "persist": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["run_id"] is None
    assert env.created == []


def test_store_failure_still_returns_the_result(client: TestClient, env: _Env) -> None:
    env.create_error = RuntimeError('column "engine" of relation "backtest_run" does not exist')
    resp = client.post(_PATH, json=_BODY)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["run"]["persisted"] is False
    assert "engine" in data["run"]["error"]
    assert data["summary"]["total_pnl"] == 42.0


@pytest.mark.parametrize(
    "patch",
    [
        {"symbols": []},
        {"symbols": [f"S{i}" for i in range(11)]},
        {"structure": "naked_call"},
        {"start": "2025-06-01", "end": "2025-01-01"},
        {"start": "2015-01-01", "end": "2025-01-01"},
        {"short_delta": 0.9},
        {"unknown_field": 1},
    ],
)
def test_bad_bodies_are_422(client: TestClient, env: _Env, patch: dict[str, Any]) -> None:
    resp = client.post(_PATH, json={**_BODY, **patch})
    assert resp.status_code == 422, resp.text
    assert env.sims == []


def test_window_defaults_to_two_years(client: TestClient, env: _Env) -> None:
    resp = client.post(_PATH, json={"symbols": ["SPY"], "end": "2026-10-01"})
    assert resp.status_code == 200, resp.text
    _, start, end, _ = env.sims[0]
    assert (end - start).days == 730


def test_structures_lists_legs(client: TestClient) -> None:
    resp = client.get("/research/backtest/sim/structures")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert set(data) == {"short_put", "put_credit_spread", "call_credit_spread", "short_strangle", "iron_condor"}
    assert len(data["iron_condor"]) == 4


def test_anonymous_run_is_refused(client: TestClient, env: _Env, auth_on: None) -> None:
    resp = client.post(_PATH, json=_BODY)
    assert resp.status_code == 401, resp.text
    assert env.sims == [] and env.created == []


def test_detail_404_for_unknown_run(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_MOD}.connect", lambda: MagicMock())
    monkeypatch.setattr(f"{_MOD}.repo.get_run", lambda conn, rid: None)
    resp = client.get("/research/backtest/sim/bt_nope/detail")
    assert resp.status_code == 404, resp.text


def test_delta_tolerance_reaches_the_simulator_and_a_signal_offset_below_zero_is_refused(client: TestClient, env: _Env) -> None:
    resp = client.post(_PATH, json={**_BODY, "delta_tolerance": 0.03})
    assert resp.status_code == 200, resp.text
    assert env.sims[0][3].delta_tolerance == 0.03
    assert env.sims[0][3].to_dict()["delta_tolerance"] == 0.03
    resp = client.post(_PATH, json={**_BODY, "delta_tolerance": None})
    assert resp.status_code == 200 and env.sims[1][3].delta_tolerance is None
    pine = {"kind": "pine_signal", "params": {"script": "supertrend", "side": "buy"}}
    resp = client.post(_PATH, json={**_BODY, "entry_event": pine, "entry_offset_sessions": -1})
    assert resp.status_code == 422 and "0 = the session after the signal" in resp.text
    resp = client.post(_PATH, json={**_BODY, "entry_event": pine, "entry_offset_sessions": 0})
    assert resp.status_code == 200, resp.text



def test_a_signal_kind_defaults_to_the_next_session_and_a_dated_event_to_the_one_before(client: TestClient, env: _Env) -> None:
    for kind, want in (("sepa_hit", 0), ("iv_percentile_threshold", 0), ("pine_signal", 0), ("earnings", -1), ("opex", -1)):
        resp = client.post(_PATH, json={**_BODY, "entry_event": {"kind": kind}})
        assert resp.status_code == 200, resp.text
        assert env.sims[-1][3].entry_offset_sessions == want, kind
    assert client.post(_PATH, json=_BODY).status_code == 200
    assert env.sims[-1][3].entry_offset_sessions == -1  # no event: unused, unchanged
    for kind in ("sepa_hit", "iv_percentile_threshold"):
        resp = client.post(_PATH, json={**_BODY, "entry_event": {"kind": kind}, "entry_offset_sessions": -1})
        assert resp.status_code == 422 and "0 = the session after the signal" in resp.text


_PINE = {"kind": "pine_signal", "params": {"script": "supertrend", "side": "buy"}}


def test_pine_exit_and_strike_anchor_reach_the_simulator(client: TestClient, env: _Env) -> None:
    body = {
        **_BODY,
        "entry_event": _PINE,
        "entry_offset_sessions": 0,
        "pine_exit": "auto",
        "strike_anchor": {"plot": "Supertrend", "max_delta": 0.35},
    }
    resp = client.post(_PATH, json=body)
    assert resp.status_code == 200, resp.text
    cfg = env.sims[0][3]
    assert cfg.pine_exit == "auto"
    assert cfg.strike_anchor == {"plot": "Supertrend", "min_delta": 0.05, "max_delta": 0.35}
    assert env.created[0]["params"]["strike_anchor"]["plot"] == "Supertrend"
    # omitted: off, and the stored params say so
    resp = client.post(_PATH, json=_BODY)
    assert env.sims[1][3].pine_exit is None and env.sims[1][3].strike_anchor is None


@pytest.mark.parametrize(
    "patch",
    [
        {"pine_exit": "auto"},  # no Pine entry
        {"entry_event": {"kind": "earnings"}, "strike_anchor": {"plot": "x"}},
        {"entry_event": _PINE, "entry_offset_sessions": 0, "pine_exit": "sometimes"},
        {"entry_event": _PINE, "entry_offset_sessions": 0, "strike_anchor": {"plot": ""}},
        {"entry_event": _PINE, "entry_offset_sessions": 0, "strike_anchor": {"plot": "x", "min_delta": 0.3, "max_delta": 0.2}},
    ],
)
def test_bad_pine_options_are_422(client: TestClient, env: _Env, patch: dict[str, Any]) -> None:
    resp = client.post(_PATH, json={**_BODY, **patch})
    assert resp.status_code == 422, resp.text
    assert env.sims == []


def test_runner_down_is_503_and_a_bad_pine_request_400(client: TestClient, env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from bifrost_research.engines.backtest.sim.pine import PineRunnerUnavailable

    def down(*a: Any, **k: Any) -> SimResult:
        raise PineRunnerUnavailable("pine-runner: URLError: refused")

    monkeypatch.setattr(f"{_MOD}.run_sim", down)
    body = {**_BODY, "entry_event": _PINE, "entry_offset_sessions": 0, "pine_exit": "auto"}
    resp = client.post(_PATH, json=body)
    assert resp.status_code == 503 and "refused" in resp.text

    def bad(*a: Any, **k: Any) -> SimResult:
        raise ValueError("strike_anchor places one short strike; short_strangle picks 2 legs by delta")

    monkeypatch.setattr(f"{_MOD}.run_sim", bad)
    resp = client.post(_PATH, json=body)
    assert resp.status_code == 400 and "picks 2 legs" in resp.text
