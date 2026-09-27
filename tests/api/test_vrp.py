"""VRP HTTP route tests — Wave RS-B-VRP2."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api import vrp as vrp_api
from bifrost_research.api.app import create_app


class _StubConn:
    def close(self) -> None:  # pragma: no cover — closes cleanly
        return None


def _patch(
    monkeypatch,
    *,
    latest=None,
    history=None,
    extremes=None,
    left_out=None,
    ranked=0,
    as_of="2026-08-25",
    seen=None,
):
    monkeypatch.setattr(vrp_api, "_connect_or_503", lambda: _StubConn())

    from bifrost_research.repositories import vrp as repo

    monkeypatch.setattr(repo, "get_latest", lambda _c, _s: latest)
    monkeypatch.setattr(repo, "get_history", lambda _c, _s, days=252: list(history or []))

    def _extremes(_c, *, as_of, bucket, limit):
        if seen is not None:
            seen["as_of"] = as_of
        return list(extremes or [])

    monkeypatch.setattr(repo, "get_extremes", _extremes)
    monkeypatch.setattr(repo, "get_left_out", lambda _c, *, as_of: list(left_out or []))
    monkeypatch.setattr(repo, "count_ranked", lambda _c, *, as_of: ranked)
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
    left_out = [{"symbol": "AMD", "trade_date": "2026-07-30", "reason": "not_computed"}]
    seen: dict[str, Any] = {}
    _patch(monkeypatch, extremes=rows, left_out=left_out, ranked=58, seen=seen)
    with _client() as c:
        r = c.get("/research/vrp/extremes?bucket=high&limit=2")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["data"]["bucket"] == "high"
    assert j["data"]["limit"] == 2
    assert j["data"]["as_of"] == "2026-08-25"
    assert len(j["data"]["rows"]) == 2
    # Ranked on the as_of session, handed down as a date.
    assert seen["as_of"] == date(2026, 8, 25)
    assert j["data"]["ranked"] == 58
    assert j["data"]["excluded"] == left_out
    assert j["data"]["excluded_count"] == 1


def test_extremes_without_any_row(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    _patch(monkeypatch, extremes=[{"symbol": "TSLA"}], as_of=None, seen=seen)
    with _client() as c:
        r = c.get("/research/vrp/extremes?bucket=low")
    d = r.json()["data"]
    assert (d["rows"], d["ranked"], d["excluded"], d["as_of"]) == ([], 0, [], None)
    assert seen == {}
    assert d["retired_count"] == 0


def test_extremes_rejects_unknown_bucket() -> None:
    with _client() as c:
        r = c.get("/research/vrp/extremes?bucket=middle")
    assert r.status_code == 422


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
    """Guard: `/research/vrp/*` prefix must be part of create_app()."""
    paths = _collect_paths(create_app())
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
    assert repo.get_extremes(_Conn([]), as_of=date(2026, 8, 25), bucket="high", limit=5) == []
    assert repo.get_left_out(_Conn([]), as_of=date(2026, 8, 25)) == []
    assert repo.count_ranked(_Conn(None), as_of=date(2026, 8, 25)) == 0
    assert repo.latest_trade_date(_Conn(None)) is None


class _ScriptedCur:
    """Answers each execute with the next scripted result; records SQL + params."""

    def __init__(self, conn: "_ScriptedConn") -> None:
        self._conn = conn
        self._rows: list[Any] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._conn.calls.append((sql, params))
        self._rows = self._conn.results.pop(0) if self._conn.results else []

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def __enter__(self) -> "_ScriptedCur":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _ScriptedConn:
    def __init__(self, *results: list[Any]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self) -> _ScriptedCur:
        return _ScriptedCur(self)


def test_extremes_rank_one_session_only() -> None:
    from bifrost_research.repositories import vrp as repo

    conn = _ScriptedConn([("NVDA", date(2026, 8, 25))])
    rows = repo.get_extremes(conn, as_of=date(2026, 8, 25), bucket="low", limit=3)
    assert rows[0]["trade_date"] == "2026-08-25"
    sql, params = conn.calls[0]
    assert "trade_date = %s" in sql
    assert "ORDER BY vrp_pct_252d ASC" in sql
    assert params == (date(2026, 8, 25), 3)


def test_left_out_names_the_reason() -> None:
    from bifrost_research.repositories import vrp as repo

    conn = _ScriptedConn(
        [
            ("AMD", date(2026, 8, 21), True, False),
            ("INTC", date(2026, 7, 30), False, False),
            ("ZZQ", date(2026, 6, 2), False, True),
        ]
    )
    out = repo.get_left_out(conn, as_of=date(2026, 8, 25))
    assert out == [
        {"symbol": "AMD", "trade_date": "2026-08-21", "reason": "no_percentile"},
        {"symbol": "INTC", "trade_date": "2026-07-30", "reason": "not_computed"},
        {"symbol": "ZZQ", "trade_date": "2026-06-02", "reason": "retired"},
    ]
    sql, params = conn.calls[0]
    # Retired asks the ticker flag and the closes together, as of the session.
    assert "t.active IS FALSE" in sql and "s.bar_date > %s" in sql
    assert params == (date(2026, 8, 25), date(2026, 8, 11), date(2026, 8, 25))


def test_extremes_keep_retired_listings_off_the_list(monkeypatch) -> None:
    left_out = [
        {"symbol": "ZZR", "trade_date": "2026-08-20", "reason": "not_computed"},
        {"symbol": "ZZQ", "trade_date": "2026-06-02", "reason": "retired"},
        {"symbol": "ZZS", "trade_date": "2026-05-11", "reason": "retired"},
    ]
    _patch(monkeypatch, extremes=[], left_out=left_out, ranked=4)
    with _client() as c:
        d = c.get("/research/vrp/extremes?bucket=high").json()["data"]
    assert [e["symbol"] for e in d["excluded"]] == ["ZZR"]
    assert d["excluded_count"] == 1
    assert d["retired_count"] == 2


def test_extremes_payload_rejects_unknown_bucket() -> None:
    from bifrost_research.repositories import vrp as repo

    conn = _ScriptedConn()
    with pytest.raises(ValueError):
        repo.extremes_payload(conn, bucket="middle")
    assert conn.calls == []
