"""VRP HTTP route tests — Wave RS-B-VRP2."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from bifrost_research.api import vrp as vrp_api
from bifrost_research.api.app import create_app


class _StubConn:
    def close(self) -> None:  # pragma: no cover — closes cleanly
        return None


def _patch(monkeypatch, *, latest=None, history=None, extremes=None, as_of="2026-08-25"):
    monkeypatch.setattr(vrp_api, "_connect_or_503", lambda: _StubConn())

    from bifrost_research.repositories import vrp as repo

    monkeypatch.setattr(repo, "get_latest", lambda _c, _s: latest)
    monkeypatch.setattr(repo, "get_history", lambda _c, _s, days=252: list(history or []))

    def _extremes(_c, *, bucket, limit):
        return list(extremes or [])

    monkeypatch.setattr(repo, "get_extremes", _extremes)
    monkeypatch.setattr(repo, "latest_trade_date", lambda _c: as_of)


def _client() -> TestClient:
    return TestClient(create_app())


def test_latest_returns_row(monkeypatch) -> None:
    latest_row = {
        "symbol": "NVDA",
        "trade_date": "2026-08-25",
        "rv_20d": 0.35,
        "rv_60d": 0.32,
        "rv_252d": 0.30,
        "atm_iv_30d": 0.42,
        "vrp_20d": 0.07,
        "vrp_60d": 0.10,
        "vrp_pct_252d": 85.0,
        "fwd_ret_20d": None,
        "computed_at": "2026-08-25T23:15:00+00:00",
    }
    _patch(monkeypatch, latest=latest_row)
    with _client() as c:
        r = c.get("/research/vrp/latest?symbol=nvda")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["data"]["row"]["symbol"] == "NVDA"
    assert j["data"]["row"]["vrp_pct_252d"] == 85.0


def test_latest_when_missing(monkeypatch) -> None:
    _patch(monkeypatch, latest=None)
    with _client() as c:
        r = c.get("/research/vrp/latest?symbol=AAPL")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["data"]["row"] is None
    assert j["data"]["symbol"] == "AAPL"


def test_latest_requires_symbol() -> None:
    with _client() as c:
        r = c.get("/research/vrp/latest")
    assert r.status_code == 422


def test_history_returns_rows(monkeypatch) -> None:
    rows = [
        {
            "symbol": "NVDA",
            "trade_date": f"2026-08-{i:02d}",
            "rv_20d": 0.3 + i * 0.001,
            "rv_60d": 0.28,
            "rv_252d": 0.25,
            "atm_iv_30d": 0.4,
            "vrp_20d": 0.1,
            "vrp_60d": 0.12,
            "vrp_pct_252d": 75.0,
            "fwd_ret_20d": None,
            "computed_at": None,
        }
        for i in range(1, 6)
    ]
    _patch(monkeypatch, history=rows)
    with _client() as c:
        r = c.get("/research/vrp/history?symbol=NVDA&days=5")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["data"]["count"] == 5
    assert j["data"]["days"] == 5
    assert j["data"]["symbol"] == "NVDA"
    assert len(j["data"]["rows"]) == 5


def test_history_rejects_invalid_days() -> None:
    with _client() as c:
        r = c.get("/research/vrp/history?symbol=NVDA&days=0")
    assert r.status_code == 422


def test_extremes_high_bucket(monkeypatch) -> None:
    rows = [
        {"symbol": "TSLA", "vrp_pct_252d": 95.0, "trade_date": "2026-08-25"},
        {"symbol": "NVDA", "vrp_pct_252d": 92.0, "trade_date": "2026-08-25"},
    ]
    _patch(monkeypatch, extremes=rows)
    with _client() as c:
        r = c.get("/research/vrp/extremes?bucket=high&limit=2")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["data"]["bucket"] == "high"
    assert j["data"]["limit"] == 2
    assert j["data"]["as_of"] == "2026-08-25"
    assert len(j["data"]["rows"]) == 2


def test_extremes_rejects_unknown_bucket() -> None:
    with _client() as c:
        r = c.get("/research/vrp/extremes?bucket=middle")
    assert r.status_code == 422


def test_router_registered_in_app() -> None:
    """Guard: `/research/vrp/*` prefix must be part of create_app()."""
    app = create_app()
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
    assert "/research/vrp/latest" in paths
    assert "/research/vrp/history" in paths
    assert "/research/vrp/extremes" in paths


def test_repository_returns_none_or_list(monkeypatch) -> None:
    """Direct repo-layer smoke: functions do not raise on empty results."""
    from bifrost_research.repositories import vrp as repo

    class _Cur:
        def __init__(self, rows: list[Any] | None) -> None:
            self._rows = rows

        def execute(self, *_a: Any, **_k: Any) -> None:
            return None

        def fetchone(self) -> Any:
            return None if not self._rows else self._rows[0]

        def fetchall(self) -> list[Any]:
            return list(self._rows or [])

        def __enter__(self) -> "_Cur":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    class _Conn:
        def __init__(self, rows: list[Any] | None) -> None:
            self._rows = rows

        def cursor(self) -> _Cur:
            return _Cur(self._rows)

    assert repo.get_latest(_Conn(None), "NVDA") is None
    assert repo.get_history(_Conn([]), "NVDA", days=5) == []
    assert repo.get_extremes(_Conn([]), bucket="high", limit=5) == []
    assert repo.latest_trade_date(_Conn(None)) is None
