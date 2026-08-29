"""OpEx Cycle HTTP route tests — Wave RS-B-OpEx2."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from bifrost_research.api import opex_cycle as opex_api
from bifrost_research.api.app import create_app


class _StubConn:
    def close(self) -> None:  # pragma: no cover
        return None


def _patch(
    monkeypatch,
    *,
    current_row=None,
    strike_map=None,
    history_rows=None,
    pin_rows=None,
    as_of="2026-08-25",
):
    monkeypatch.setattr(opex_api, "_connect_or_503", lambda: _StubConn())
    from bifrost_research.repositories import opex_cycle as repo

    monkeypatch.setattr(repo, "get_current", lambda _c, _s, trade_date=None: current_row)
    monkeypatch.setattr(
        repo,
        "get_vanna_charm_map",
        lambda _c, _s, trade_date=None, limit=60: list(strike_map or []),
    )
    monkeypatch.setattr(
        repo,
        "get_history",
        lambda _c, _s, *, cycles=12: list(history_rows or []),
    )
    monkeypatch.setattr(
        repo,
        "get_pin_analysis",
        lambda _c, _s, *, cycles=24: list(pin_rows or []),
    )
    monkeypatch.setattr(repo, "latest_trade_date", lambda _c: as_of)


def _client() -> TestClient:
    return TestClient(create_app())


def test_current_returns_row(monkeypatch) -> None:
    row = {
        "symbol": "SPX",
        "trade_date": "2026-08-25",
        "spot": 5400.0,
        "total_vanna": 12345.6,
        "total_charm": -789.0,
        "vanna_zero_strike": 5300.0,
        "charm_zero_strike": 5450.0,
        "dte_to_opex": 24,
        "is_opex_week": False,
    }
    strike_map = [
        {"strike": 5350.0, "call_oi": 100, "put_oi": 200, "net_gex": -1.5},
        {"strike": 5400.0, "call_oi": 300, "put_oi": 250, "net_gex": 2.0},
    ]
    _patch(monkeypatch, current_row=row, strike_map=strike_map)
    with _client() as c:
        r = c.get("/research/opex-cycle/current?symbol=SPX")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["data"]["row"]["symbol"] == "SPX"
    assert j["data"]["row"]["total_vanna"] == 12345.6
    assert len(j["data"]["strike_map"]) == 2
    # Calendar helpers always populated
    assert isinstance(j["data"]["dte_to_opex_today"], int)
    assert isinstance(j["data"]["is_opex_week_today"], bool)
    assert j["data"]["next_opex_date"]


def test_current_requires_symbol() -> None:
    with _client() as c:
        r = c.get("/research/opex-cycle/current")
    assert r.status_code == 422


def test_current_include_map_false(monkeypatch) -> None:
    _patch(monkeypatch, current_row={"symbol": "SPX"}, strike_map=[{"strike": 100.0}])
    with _client() as c:
        r = c.get("/research/opex-cycle/current?symbol=SPX&include_map=false")
    assert r.status_code == 200
    assert r.json()["data"]["strike_map"] == []


def test_current_invalid_date(monkeypatch) -> None:
    _patch(monkeypatch)
    with _client() as c:
        r = c.get("/research/opex-cycle/current?symbol=SPX&trade_date=not-a-date")
    assert r.status_code == 400


def test_history_returns_cycles(monkeypatch) -> None:
    rows = [
        {"symbol": "SPX", "trade_date": "2026-06-18", "opex_date": "2026-06-19", "spot": 5300.0},
        {"symbol": "SPX", "trade_date": "2026-07-16", "opex_date": "2026-07-17", "spot": 5350.0},
        {"symbol": "SPX", "trade_date": "2026-08-20", "opex_date": "2026-08-21", "spot": 5400.0},
    ]
    _patch(monkeypatch, history_rows=rows)
    with _client() as c:
        r = c.get("/research/opex-cycle/history?symbol=SPX&cycles=3")
    assert r.status_code == 200
    j = r.json()
    assert j["data"]["count"] == 3
    assert j["data"]["cycles_requested"] == 3


def test_history_cycles_bounds() -> None:
    with _client() as c:
        r_low = c.get("/research/opex-cycle/history?symbol=SPX&cycles=0")
        r_high = c.get("/research/opex-cycle/history?symbol=SPX&cycles=999")
    assert r_low.status_code == 422
    assert r_high.status_code == 422


def test_pin_analysis(monkeypatch) -> None:
    rows = [
        {
            "opex_date": "2026-06-19",
            "expiry": "2026-06-19",
            "max_pain_strike": 5300.0,
            "settle_close": 5302.0,
            "distance": 2.0,
            "pct_distance": 0.000377,
            "total_oi": 100000,
        },
        {
            "opex_date": "2026-07-17",
            "expiry": "2026-07-17",
            "max_pain_strike": 5350.0,
            "settle_close": 5400.0,
            "distance": 50.0,
            "pct_distance": 0.00935,
            "total_oi": 120000,
        },
    ]
    _patch(monkeypatch, pin_rows=rows)
    with _client() as c:
        r = c.get("/research/opex-cycle/pin-analysis?symbol=SPX&cycles=2")
    assert r.status_code == 200
    j = r.json()
    assert j["data"]["count"] == 2
    # 1 out of 2 near pin
    assert j["data"]["pin_rate"] == 0.5


def test_pin_analysis_empty_pin_rate(monkeypatch) -> None:
    _patch(monkeypatch, pin_rows=[])
    with _client() as c:
        r = c.get("/research/opex-cycle/pin-analysis?symbol=SPX")
    assert r.status_code == 200
    assert r.json()["data"]["pin_rate"] is None


def _collect_paths(app) -> set[str]:
    """FastAPI ≥0.115 wraps included routers in `_IncludedRouter`; walk both
    `.routes` (regular Mount/Router) and `.original_router.routes` (IncludedRouter)."""
    paths: set[str] = set()

    def _walk(routes) -> None:
        for r in routes:
            p = getattr(r, "path", None)
            if isinstance(p, str):
                paths.add(p)
            sub = getattr(r, "routes", None)
            if sub is None:
                orig = getattr(r, "original_router", None)
                if orig is not None:
                    sub = getattr(orig, "routes", None)
            if sub:
                _walk(sub)

    _walk(app.routes)
    return paths


def test_router_registered_in_app() -> None:
    paths = _collect_paths(create_app())
    for suffix in ("current", "history", "pin-analysis"):
        assert f"/research/opex-cycle/{suffix}" in paths


def test_repository_returns_empty_when_no_data(monkeypatch) -> None:
    from bifrost_research.repositories import opex_cycle as repo

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

    assert repo.get_current(_Conn(None), "SPX") is None
    assert repo.get_vanna_charm_map(_Conn(None), "SPX") == []
    # history / pin_analysis need at least the enumeration path; empty DB returns []
    assert repo.get_history(_Conn(None), "SPX", cycles=3) == []
    assert repo.get_pin_analysis(_Conn(None), "SPX", cycles=3) == []
    assert repo.latest_trade_date(_Conn(None)) is None
