"""IV cone: constant-maturity ATM IV per horizon against its own year (2026-09-26)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi.testclient import TestClient

from bifrost_research.api import iv_cone as cone_api
from bifrost_research.api.app import create_app
from bifrost_research.repositories import iv_cone as repo

D = date(2026, 9, 25)


def test_30_days_is_the_vrp_stores_iv30_and_may_be_one_sided() -> None:
    # Only a 20-DTE expiry: IV30 takes the nearest, as atm_iv_30d does.
    assert repo.tenor_iv(D, [(D + timedelta(days=20), 0.40)], 30) == 0.4
    # Bracketed: linear between 20 and 40 DTE.
    assert repo.tenor_iv(D, [(D + timedelta(days=20), 0.40), (D + timedelta(days=40), 0.50)], 30) == 0.45


def test_7_days_is_iv30s_rule_at_7_with_the_floor_at_the_shortest_expiry() -> None:
    # Bracketed by a 3-DTE weekly and a 10-DTE one: linear between them.
    pairs = [(D + timedelta(days=3), 0.30), (D + timedelta(days=10), 0.44)]
    assert repo.tenor_iv(D, pairs, 7) == 0.38
    assert not repo.is_one_sided(D, pairs, 7)
    # A monthly-only name: the nearest expiry past 7 days stands in, and says so.
    monthly = [(D + timedelta(days=16), 0.41), (D + timedelta(days=44), 0.47)]
    assert repo.tenor_iv(D, monthly, 7) == 0.41
    assert repo.is_one_sided(D, monthly, 7)
    # Past three times the horizon is not a 7-day reading; a same-day expiry is not either.
    assert repo.tenor_iv(D, [(D + timedelta(days=25), 0.4)], 7) is None
    assert repo.tenor_iv(D, [(D, 0.9)], 7) is None


def test_60_and_90_days_need_an_expiry_on_each_side() -> None:
    # Store chains end near 72 DTE: 90 days is not read from 45 and 72 alone.
    pairs = [(D + timedelta(days=45), 0.50), (D + timedelta(days=72), 0.55)]
    assert repo.tenor_iv(D, pairs, 90) is None
    got = repo.tenor_iv(D, pairs, 60)
    assert got is not None and abs(got - (0.50 + 0.05 * 15 / 27)) < 1e-6
    # Past three times the horizon is a different tenor, not a bracket.
    assert repo.tenor_iv(D, [(D + timedelta(days=45), 0.5), (D + timedelta(days=200), 0.6)], 60) is None


def test_quantile_interpolates() -> None:
    assert repo.quantile([0.1, 0.2, 0.3, 0.4, 0.5], 0.5) == 0.3
    assert repo.quantile([0.1, 0.2], 0.9) == 0.19
    assert repo.quantile([], 0.5) is None


def _rows(n_days: int, *, with_far: int = 0) -> list[tuple[Any, Any, Any]]:
    rows: list[tuple[Any, Any, Any]] = []
    for i in range(n_days):
        td = D - timedelta(days=n_days - 1 - i)
        iv = 0.30 + 0.001 * i  # rising: today is the year's high
        rows.append((td, td + timedelta(days=25), iv))
        rows.append((td, td + timedelta(days=65), iv + 0.02))
        if i >= n_days - with_far:
            rows.append((td, td + timedelta(days=120), iv + 0.03))
    return rows


def test_cone_reads_percentiles_and_withholds_a_thin_horizon() -> None:
    cone = repo.build_cone(_rows(100, with_far=10), window_sessions=252, min_sessions=60)
    assert cone["as_of"] == D.isoformat() and cone["sessions_in_window"] == 100
    t7, t30, t60, t90 = cone["tenors"]
    assert [t["tenor_days"] for t in cone["tenors"]] == [7, 30, 60, 90]
    # Nearest expiry is 25 DTE, past the 7-day reach of 21: not read, so withheld.
    assert t7["n"] == 0 and t7["p50"] is None and "read on 0 of 100 sessions" in t7["withheld"]
    assert t30["n"] == 100 and t30["one_sided"] == 0 and t60["one_sided"] == 0 and t30["today_pctile"] == 1.0
    assert t30["p10"] < t30["p50"] < t30["p90"] <= t30["max"]
    assert t60["n"] == 100 and t60["withheld"] is None
    assert t90["n"] == 10 and t90["p50"] is None and t90["today"] is not None
    assert "read on 10 of 100 sessions" in t90["withheld"]


def test_cone_counts_one_sided_7_day_sessions() -> None:
    rows: list[tuple[Any, Any, Any]] = []
    for i in range(80):
        td = D - timedelta(days=79 - i)
        rows.append((td, td + timedelta(days=12), 0.35))
        rows.append((td, td + timedelta(days=40), 0.38))
        if i % 2 == 0:  # a weekly every other session
            rows.append((td, td + timedelta(days=4), 0.33))
    t7 = repo.build_cone(rows, min_sessions=60)["tenors"][0]
    assert t7["tenor_days"] == 7 and t7["n"] == 80 and t7["one_sided"] == 40
    assert t7["withheld"] is None and "7 DTE" in t7["rule"]


def test_cone_window_keeps_the_newest_sessions() -> None:
    cone = repo.build_cone(_rows(300), window_sessions=252)
    assert cone["sessions_in_window"] == 252
    assert cone["tenors"][1]["first"] == (D - timedelta(days=251)).isoformat()


def test_route_404s_a_name_with_no_rows(monkeypatch) -> None:
    class _Conn:
        def close(self) -> None:
            return None

    monkeypatch.setattr(cone_api, "connect", lambda: _Conn())
    monkeypatch.setattr(
        cone_api.repo, "fetch_cone", lambda conn, symbol, *, as_of, window_sessions: {"symbol": "CUE", "as_of": None}
    )
    assert TestClient(create_app()).get("/research/volatility/iv-cone?symbol=CUE").status_code == 404
