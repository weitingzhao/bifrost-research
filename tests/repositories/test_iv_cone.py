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
    t30, t60, t90 = cone["tenors"]
    assert t30["n"] == 100 and t30["today_pctile"] == 1.0
    assert t30["p10"] < t30["p50"] < t30["p90"] <= t30["max"]
    assert t60["n"] == 100 and t60["withheld"] is None
    assert t90["n"] == 10 and t90["p50"] is None and t90["today"] is not None
    assert "read on 10 of 100 sessions" in t90["withheld"]


def test_cone_window_keeps_the_newest_sessions() -> None:
    cone = repo.build_cone(_rows(300), window_sessions=252)
    assert cone["sessions_in_window"] == 252
    assert cone["tenors"][0]["first"] == (D - timedelta(days=251)).isoformat()


def test_route_404s_a_name_with_no_rows(monkeypatch) -> None:
    class _Conn:
        def close(self) -> None:
            return None

    monkeypatch.setattr(cone_api, "connect", lambda: _Conn())
    monkeypatch.setattr(
        cone_api.repo, "fetch_cone", lambda conn, symbol, *, as_of, window_sessions: {"symbol": "CUE", "as_of": None}
    )
    assert TestClient(create_app()).get("/research/volatility/iv-cone?symbol=CUE").status_code == 404
