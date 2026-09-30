"""Vol Surface HTTP route tests — Wave RS-B-Surface2."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi.testclient import TestClient

from bifrost_research.api import vol_surface as vol_surface_api
from bifrost_research.api.app import create_app


class _StubConn:
    def close(self) -> None:  # pragma: no cover
        return None


def _patch(
    monkeypatch,
    *,
    fit_rows=None,
    term_rows=None,
    residual_rows=None,
    skew_rows=None,
    left_out=None,
    ranked=0,
    as_of="2026-08-25",
    seen=None,
):
    monkeypatch.setattr(vol_surface_api, "_connect_or_503", lambda: _StubConn())
    from bifrost_research.repositories import vol_surface as repo

    monkeypatch.setattr(repo, "get_fit", lambda _c, _s, trade_date=None: list(fit_rows or []))
    monkeypatch.setattr(
        repo,
        "get_term_structure",
        lambda _c, _s, trade_date=None: list(term_rows or []),
    )
    monkeypatch.setattr(
        repo,
        "get_residuals",
        lambda _c, _s, _exp, trade_date=None: list(residual_rows or []),
    )
    def _skew(_c, *, as_of, limit=20):
        if seen is not None:
            seen["as_of"] = as_of
        return list(skew_rows or [])

    monkeypatch.setattr(repo, "get_skew_extremes", _skew)
    monkeypatch.setattr(repo, "get_skew_left_out", lambda _c, *, as_of: list(left_out or []))
    monkeypatch.setattr(repo, "count_skew_names", lambda _c, *, as_of: ranked)
    monkeypatch.setattr(repo, "latest_trade_date", lambda _c: as_of)


def _client() -> TestClient:
    return TestClient(create_app())


def test_fit_returns_rows(monkeypatch) -> None:
    rows = [
        {
            "symbol": "NVDA",
            "trade_date": "2026-08-25",
            "expiry": "2026-09-19",
            "dte": 25,
            "svi_a": 0.04,
            "svi_b": 0.4,
            "svi_rho": -0.3,
            "svi_m": 0.0,
            "svi_sigma": 0.1,
            "atm_vol": 0.32,
            "atm_slope": -0.12,
            "fit_rmse": 0.008,
            "n_points": 22,
            "computed_at": None,
        }
    ]
    _patch(monkeypatch, fit_rows=rows)
    with _client() as c:
        r = c.get("/research/vol-surface/fit?symbol=NVDA")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["data"]["count"] == 1
    assert j["data"]["rows"][0]["expiry"] == "2026-09-19"


def test_fit_requires_symbol() -> None:
    with _client() as c:
        r = c.get("/research/vol-surface/fit")
    assert r.status_code == 422


def test_fit_invalid_date(monkeypatch) -> None:
    _patch(monkeypatch)
    with _client() as c:
        r = c.get("/research/vol-surface/fit?symbol=NVDA&trade_date=notadate")
    assert r.status_code == 400


def test_term_structure_returns_rows(monkeypatch) -> None:
    rows = [
        {"expiry": "2026-09-19", "dte": 25, "atm_vol": 0.30, "atm_slope": -0.1, "fit_rmse": 0.008, "n_points": 20},
        {"expiry": "2026-10-17", "dte": 53, "atm_vol": 0.28, "atm_slope": -0.09, "fit_rmse": 0.009, "n_points": 18},
    ]
    _patch(monkeypatch, term_rows=rows)
    with _client() as c:
        r = c.get("/research/vol-surface/term-structure?symbol=NVDA")
    assert r.status_code == 200
    j = r.json()
    assert j["data"]["count"] == 2
    assert [row["dte"] for row in j["data"]["rows"]] == [25, 53]


def test_residuals_requires_expiry() -> None:
    with _client() as c:
        r = c.get("/research/vol-surface/residuals?symbol=NVDA")
    assert r.status_code == 422


def test_residuals_returns_rows(monkeypatch) -> None:
    rows = [
        {
            "symbol": "NVDA",
            "trade_date": "2026-08-25",
            "expiry": "2026-09-19",
            "strike": 100.0,
            "log_moneyness": -0.05,
            "iv_market": 0.35,
            "iv_fitted": 0.34,
            "residual": 0.01,
            "residual_z": 1.25,
            "computed_at": None,
        }
    ]
    _patch(monkeypatch, residual_rows=rows)
    with _client() as c:
        r = c.get(
            "/research/vol-surface/residuals?symbol=NVDA&expiry=2026-09-19"
        )
    assert r.status_code == 200
    j = r.json()
    assert j["data"]["count"] == 1
    assert j["data"]["rows"][0]["strike"] == 100.0


def test_skew_extremes(monkeypatch) -> None:
    rows = [
        {"symbol": "TSLA", "atm_slope": -0.35, "dte": 30, "atm_vol": 0.45, "trade_date": "2026-08-25"},
        {"symbol": "NVDA", "atm_slope": 0.30, "dte": 30, "atm_vol": 0.35, "trade_date": "2026-08-25"},
    ]
    left_out = [
        {"symbol": "AMD", "trade_date": "2026-08-24", "reason": "no_30d_fit"},
        {"symbol": "INTC", "trade_date": "2026-08-12", "reason": "not_fit"},
    ]
    seen: dict[str, Any] = {}
    _patch(monkeypatch, skew_rows=rows, left_out=left_out, ranked=37, seen=seen)
    with _client() as c:
        r = c.get("/research/vol-surface/skew-extremes?limit=5")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["count"] == 2
    assert d["limit"] == 5
    assert d["as_of"] == "2026-08-25"
    # Ranked on the as_of session, handed down as a date.
    assert seen["as_of"] == date(2026, 8, 25)
    assert d["ranked"] == 37
    assert d["excluded_count"] == 2
    assert [e["reason"] for e in d["excluded"]] == ["no_30d_fit", "not_fit"]


def test_skew_extremes_without_any_fit(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    _patch(monkeypatch, skew_rows=[{"symbol": "TSLA"}], as_of=None, seen=seen)
    with _client() as c:
        r = c.get("/research/vol-surface/skew-extremes")
    d = r.json()["data"]
    assert (d["rows"], d["ranked"], d["excluded"], d["as_of"]) == ([], 0, [], None)
    assert seen == {}


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


def test_skew_extremes_ranks_one_session_only() -> None:
    from bifrost_research.lenses.slope_tenor import SLOPE_30D_WHERE_BINDS
    from bifrost_research.repositories import vol_surface as repo

    conn = _ScriptedConn([("TSLA", date(2026, 8, 25), date(2026, 9, 26), 32)])
    rows = repo.get_skew_extremes(conn, as_of=date(2026, 8, 25), limit=500)
    assert rows[0]["symbol"] == "TSLA" and rows[0]["trade_date"] == "2026-08-25"
    # The SVI parameters stay as null keys for one release (no single smile).
    assert "svi_a" in rows[0] and rows[0]["svi_a"] is None
    sql, params = conn.calls[0]
    assert "trade_date = %s" in sql
    assert "dte BETWEEN 20 AND 45" in sql and "'interpolated'" in sql
    # The ~30-day reading repeats its where (lenses/slope_tenor.py).
    assert params == (date(2026, 8, 25),) * SLOPE_30D_WHERE_BINDS + (200,)


def test_skew_left_out_names_the_reason() -> None:
    from bifrost_research.repositories import vol_surface as repo

    conn = _ScriptedConn(
        [
            ("AMD", date(2026, 8, 24), True, False),
            ("INTC", date(2026, 8, 12), False, False),
            ("ZZQ", date(2026, 7, 9), False, True),
        ]
    )
    out = repo.get_skew_left_out(conn, as_of=date(2026, 8, 25))
    assert out == [
        {"symbol": "AMD", "trade_date": "2026-08-24", "reason": "no_30d_fit"},
        {"symbol": "INTC", "trade_date": "2026-08-12", "reason": "not_fit"},
        {"symbol": "ZZQ", "trade_date": "2026-07-09", "reason": "retired"},
    ]
    sql, params = conn.calls[0]
    assert "l.trade_date < %s" in sql
    assert "t.active IS FALSE" in sql and "s.bar_date > %s" in sql
    assert params == (date(2026, 8, 25), date(2026, 8, 11), date(2026, 8, 25))


def test_skew_payload_reads_one_session() -> None:
    from bifrost_research.repositories import vol_surface as repo

    conn = _ScriptedConn(
        [(date(2026, 8, 25),)],
        [("TSLA", date(2026, 8, 25), date(2026, 9, 26), 32)],
        [("AMD", date(2026, 8, 24), False, False), ("ZZQ", date(2026, 7, 9), False, True)],
        [(41,)],
    )
    d = repo.skew_extremes_payload(conn, limit=5)
    assert d["as_of"] == "2026-08-25"
    assert [r["symbol"] for r in d["rows"]] == ["TSLA"]
    assert d["ranked"] == 41
    # A retired listing is counted, not listed: nothing bounds how far back the
    # left-out read looks, so a listed one would stay on the list for good.
    assert d["excluded_count"] == 1 and d["excluded"][0]["reason"] == "not_fit"
    assert d["retired_count"] == 1
    assert all(p[0] == date(2026, 8, 25) for _, p in conn.calls[1:])


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
    for suffix in ("fit", "term-structure", "residuals", "skew-extremes"):
        assert f"/research/vol-surface/{suffix}" in paths


def test_repository_returns_empty_when_no_data(monkeypatch) -> None:
    from bifrost_research.repositories import vol_surface as repo

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

    # get_fit with no trade_date, empty MAX() → resolves to None → early return
    assert repo.get_fit(_Conn(None), "NVDA") == []
    assert repo.get_term_structure(_Conn(None), "NVDA") == []
    assert repo.get_residuals(_Conn(None), "NVDA", date(2026, 9, 19)) == []
    assert repo.get_skew_extremes(_Conn([]), as_of=date(2026, 8, 25), limit=5) == []
    assert repo.get_skew_left_out(_Conn([]), as_of=date(2026, 8, 25)) == []
    assert repo.count_skew_names(_Conn(None), as_of=date(2026, 8, 25)) == 0
    assert repo.latest_trade_date(_Conn(None)) is None
