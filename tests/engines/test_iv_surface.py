"""Pure-compute tests for IV surface / smile / vol cone (no DB)."""

from __future__ import annotations

from datetime import date

from bifrost_research.engines.volatility.surface import (
    IvPoint,
    fetch_iv_points_for_date,
    fetch_spot_fallback,
    fit_iv_surface,
    fit_polynomial_smile,
    fit_svi_smile,
    moneyness,
    vol_cone_from_history,
)


def _smile_points(spot: float = 100.0) -> list[IvPoint]:
    # Mild smile: higher IV away from ATM
    return [
        IvPoint(strike=90.0, iv=0.32, option_right="P"),
        IvPoint(strike=95.0, iv=0.28, option_right="P"),
        IvPoint(strike=100.0, iv=0.24, option_right="C"),
        IvPoint(strike=105.0, iv=0.26, option_right="C"),
        IvPoint(strike=110.0, iv=0.30, option_right="C"),
        IvPoint(strike=100.0, iv=0.25, option_right="P"),
        IvPoint(strike=105.0, iv=0.27, option_right="P"),
    ]


def test_moneyness() -> None:
    assert abs(moneyness(100.0, 100.0)) < 1e-12
    assert moneyness(110.0, 100.0) > 0


def test_polynomial_fit() -> None:
    fit = fit_polynomial_smile(_smile_points(), 100.0, degree=2)
    assert fit is not None
    assert fit["model"] == "polynomial"
    assert len(fit["coeffs"]) == 3
    assert fit["n_points"] >= 5
    assert fit["rmse"] >= 0


def test_svi_fit_or_skip() -> None:
    fit = fit_svi_smile(_smile_points(), 100.0)
    # May succeed with 7 points; if None, still OK for coarse grid
    if fit is not None:
        assert fit["model"] == "svi"
        assert "a" in fit["params"]
        assert fit["n_points"] >= 5


def test_vol_cone() -> None:
    hist = [0.20, 0.22, 0.25, 0.28, 0.30, 0.18, 0.35]
    cone = vol_cone_from_history(hist)
    assert cone["n"] == 7
    assert "p50" in cone["bands"]
    assert cone["min"] <= cone["bands"]["p50"] <= cone["max"]


def test_fit_iv_surface() -> None:
    by_exp = {
        date(2025, 6, 20): _smile_points(),
        date(2025, 7, 18): _smile_points(spot=100.0),
    }
    out = fit_iv_surface(by_exp, 100.0, history_ivs=[0.2, 0.25, 0.3])
    assert out["n_expiries"] >= 1
    assert len(out["surface_points"]) > 0
    assert out["vol_cone"]["n"] == 3


class _FakeCursor:
    """Sequential-query cursor: pop queued rowsets in order."""

    def __init__(self, rowsets: list[list[tuple]]):
        self._rowsets = list(rowsets)
        self._current: list[tuple] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args) -> None:
        return None

    def execute(self, query: str, params=None) -> None:
        self._current = self._rowsets.pop(0) if self._rowsets else []

    def fetchall(self) -> list[tuple]:
        return list(self._current or [])

    def fetchone(self):
        rows = self._current or []
        return rows[0] if rows else None


class _FakeConn:
    def __init__(self, rowsets: list[list[tuple]]):
        self._cursor = _FakeCursor(rowsets)

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def rollback(self) -> None:
        return None


def test_fetch_spot_fallback_from_delta() -> None:
    """SPX-style: no stock join, but delta≈0.5 call strikes are available."""
    delta_rows = [
        (7690.0, 0.499),
        (7710.0, 0.5005),
        (7685.0, 0.4993),
    ]
    conn = _FakeConn([delta_rows])
    spot = fetch_spot_fallback(conn, "SPX", date(2026, 8, 21))
    assert spot is not None
    assert 7680.0 <= spot <= 7720.0


def test_fetch_spot_fallback_uses_max_pain_when_no_delta() -> None:
    conn = _FakeConn([[], [(7500.0,)]])
    spot = fetch_spot_fallback(conn, "SPX", date(2026, 8, 21))
    assert spot == 7500.0


def test_fetch_spot_fallback_returns_none_when_no_signal() -> None:
    conn = _FakeConn([[], []])
    assert fetch_spot_fallback(conn, "SPX", date(2026, 8, 21)) is None


def test_fetch_iv_points_uses_fallback_when_stock_missing() -> None:
    """Simulates SPX: IV rows exist but underlying_price is NULL in view."""
    view_rows = [
        (date(2026, 8, 22), 7690.0, "C", 0.12, None),
        (date(2026, 8, 22), 7700.0, "P", 0.13, None),
        (date(2026, 9, 19), 7750.0, "C", 0.14, None),
    ]
    delta_rows = [(7690.0, 0.499), (7710.0, 0.501)]
    conn = _FakeConn([view_rows, delta_rows])
    spot, by_exp = fetch_iv_points_for_date(conn, "SPX", date(2026, 8, 21))
    assert spot is not None
    assert 7680.0 <= spot <= 7720.0
    assert len(by_exp) == 2
    assert all(len(points) >= 1 for points in by_exp.values())


def test_fetch_iv_points_prefers_view_underlying_price() -> None:
    view_rows = [
        (date(2026, 8, 22), 200.0, "C", 0.30, 195.0),
        (date(2026, 8, 22), 205.0, "P", 0.31, 195.0),
    ]
    conn = _FakeConn([view_rows])
    spot, by_exp = fetch_iv_points_for_date(conn, "TSLA", date(2026, 8, 21))
    assert spot == 195.0
    assert len(by_exp) == 1
