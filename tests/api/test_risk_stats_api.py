"""/analytics risk-stat routes — shape, sample guards and as_of (R9 F4, no DB)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest.mock import patch

import math
import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app

START = date(2024, 1, 2)


def _series(symbol: str, sessions: int, step: float) -> list[tuple[str, date, float]]:
    """A clean geometric series — every day's log return is exactly ``step``."""
    return [
        (symbol, START + timedelta(days=i), 100.0 * math.exp(step * i))
        for i in range(sessions)
    ]


class _Cur:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._all = rows
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        wanted = set(params[0]) if params else set()
        self._rows = [r for r in self._all if r[0] in wanted]
        if params and len(params) == 4:  # (symbols, as_of, as_of, lookback_days)
            end, lookback = params[1], params[3]
            self._rows = [r for r in self._rows if end - timedelta(days=lookback) <= r[1] <= end]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def cursor(self) -> _Cur:
        return _Cur(self.rows)

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _patch_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bifrost_research.api.health.run_startup_schema_guard", lambda: None)
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


def _client(rows: list[tuple[Any, ...]]) -> TestClient:
    return TestClient(create_app())


def _paired(sessions: int, factor: float) -> list[tuple[str, date, float]]:
    """SPY zig-zags; NVDA makes ``factor`` times each of SPY's log moves."""
    rows: list[tuple[str, date, float]] = []
    spy, nvda = 100.0, 100.0
    for i in range(sessions):
        rows.append(("SPY", START + timedelta(days=i), spy))
        rows.append(("NVDA", START + timedelta(days=i), nvda))
        step = 0.01 if i % 2 == 0 else -0.008
        spy *= math.exp(step)
        nvda *= math.exp(factor * step)
    return rows


def test_beta_answers_per_symbol_and_window_with_its_sample() -> None:
    rows = _paired(300, 2.0)
    with patch("bifrost_research.api.risk_stats.connect", return_value=_Conn(rows)):
        res = _client(rows).get(
            "/analytics/risk/beta",
            params={"symbols": "NVDA", "benchmark": "SPY", "windows": "60,252"},
        )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["benchmark"] == "SPY"
    by_window = {i["window"]: i for i in data["items"]}
    assert by_window[60]["n"] == 60 and by_window[252]["n"] == 252
    assert abs(by_window[60]["beta"] - 2.0) < 1e-9
    assert abs(by_window[252]["beta"] - 2.0) < 1e-9
    assert data["as_of"] == str(START + timedelta(days=299))


def test_a_window_the_history_cannot_fill_is_answered_null() -> None:
    rows = _paired(120, 2.0)
    with patch("bifrost_research.api.risk_stats.connect", return_value=_Conn(rows)):
        res = _client(rows).get(
            "/analytics/risk/beta",
            params={"symbols": "NVDA", "benchmark": "SPY", "windows": "60,252"},
        )
    by_window = {i["window"]: i for i in res.json()["data"]["items"]}
    assert abs(by_window[60]["beta"] - 2.0) < 1e-9
    # 119 paired returns is under 80% of 252 — no number is offered.
    assert by_window[252]["beta"] is None and by_window[252]["n"] == 119


def test_correlation_matrix_is_symmetric_and_counts_its_pairs() -> None:
    rows = _series("NVDA", 100, 0.002) + _series("MU", 100, -0.001) + _series("SPY", 40, 0.001)
    with patch("bifrost_research.api.risk_stats.connect", return_value=_Conn(rows)):
        res = _client(rows).get(
            "/analytics/risk/correlation",
            params={"symbols": "NVDA,MU,SPY", "window": "60"},
        )
    data = res.json()["data"]
    assert data["symbols"] == ["NVDA", "MU", "SPY"]
    matrix = data["matrix"]
    assert matrix["NVDA"]["NVDA"]["rho"] == 1.0
    assert matrix["NVDA"]["MU"] == matrix["MU"]["NVDA"]
    # SPY has 39 shared returns — under 80% of the 60-session window.
    assert matrix["NVDA"]["SPY"]["rho"] is None and matrix["NVDA"]["SPY"]["n"] == 39
    assert data["n_pairs"] == 1


def test_correlation_as_of_reads_nothing_after_the_date_asked_for() -> None:
    # NVDA and MU zig-zag together for 100 sessions, then MU flips phase.
    days = [START + timedelta(days=i) for i in range(160)]
    rows = [("NVDA", d, 101.0 if i % 2 else 100.0) for i, d in enumerate(days)]
    rows += [("MU", d, (101.0 if i % 2 else 100.0) if i < 100 else (100.0 if i % 2 else 101.0)) for i, d in enumerate(days)]
    client = _client(rows)
    with patch("bifrost_research.api.risk_stats.connect", return_value=_Conn(rows)):
        then = client.get(
            "/analytics/risk/correlation",
            params={"symbols": "NVDA,MU", "window": "60", "as_of": str(days[99])},
        ).json()["data"]
        now = client.get("/analytics/risk/correlation", params={"symbols": "NVDA,MU", "window": "60"}).json()["data"]
    assert then["as_of"] == str(days[99])
    assert then["matrix"]["NVDA"]["MU"]["rho"] > 0.99
    # Without as_of the window ends today and sees the flip.
    assert now["as_of"] == str(days[-1])
    assert now["matrix"]["NVDA"]["MU"]["rho"] < -0.99


def test_rv_cone_carries_current_reading_sessions_and_as_of() -> None:
    prices: list[tuple[str, date, float]] = []
    price = 100.0
    for i in range(200):
        prices.append(("NVDA", START + timedelta(days=i), price))
        price *= math.exp(0.01 if i % 2 == 0 else -0.01)
    with patch("bifrost_research.api.risk_stats.connect", return_value=_Conn(prices)):
        res = _client(prices).get(
            "/analytics/vol/rv-cone",
            params={"symbol": "nvda", "tenors": "10,20,250", "years": "3"},
        )
    data = res.json()["data"]
    assert data["symbol"] == "NVDA" and data["years"] == 3 and data["sessions"] == 200
    by_days = {t["days"]: t for t in data["tenors"]}
    assert by_days[10]["n"] > 0 and by_days[10]["p50"] is not None
    assert abs(by_days[10]["current"] - 0.01 * math.sqrt(252)) < 1e-9
    # 250 sessions of history do not exist; the tenor says so instead of shrinking.
    assert by_days[250]["n"] == 0 and by_days[250]["current"] is None
    assert data["as_of"] == str(START + timedelta(days=199))


def test_bad_input_is_rejected_not_guessed() -> None:
    client = _client([])
    with patch("bifrost_research.api.risk_stats.connect", return_value=_Conn([])):
        assert client.get("/analytics/risk/beta", params={"symbols": " "}).status_code == 400
        assert client.get(
            "/analytics/risk/beta", params={"symbols": "NVDA", "windows": "wide"}
        ).status_code == 400
        assert client.get(
            "/analytics/risk/beta", params={"symbols": "NVDA", "windows": "1"}
        ).status_code == 400
        many = ",".join(f"S{i}" for i in range(21))
        assert client.get("/analytics/risk/correlation", params={"symbols": many}).status_code == 400
        assert client.get(
            "/analytics/vol/rv-cone", params={"symbol": "NVDA", "years": "9"}
        ).status_code == 422


def test_an_empty_series_answers_with_nulls_not_an_error() -> None:
    with patch("bifrost_research.api.risk_stats.connect", return_value=_Conn([])):
        client = _client([])
        cone = client.get("/analytics/vol/rv-cone", params={"symbol": "NVDA"}).json()["data"]
        assert cone["as_of"] is None and cone["sessions"] == 0
        assert all(t["n"] == 0 and t["p50"] is None for t in cone["tenors"])
        b = client.get("/analytics/risk/beta", params={"symbols": "NVDA"}).json()["data"]
        assert b["as_of"] is None and all(i["beta"] is None and i["n"] == 0 for i in b["items"])
