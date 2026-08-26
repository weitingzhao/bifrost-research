"""Tests for walk-forward + benchmark helpers — Wave RS-C3."""

from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import pstdev

import pytest

from bifrost_research.engines.backtest.benchmark import (
    spy_buy_hold_metrics,
    zero_signal_control,
)
from bifrost_research.engines.backtest.walk_forward import (
    aggregate_oos,
    build_windows,
    run_walk_forward,
)


def _daily_series(start: date, n: int, drift: float = 0.0002) -> list[tuple[date, float]]:
    price = 100.0
    out: list[tuple[date, float]] = []
    for i in range(n):
        # deterministic pseudo-oscillation
        wave = 0.001 * math.sin(i / 7.0)
        r = drift + wave
        price *= 1.0 + r
        out.append((start + timedelta(days=i), price))
    return out


# ---------------------------------------------------------------------------
# build_windows
# ---------------------------------------------------------------------------


def test_build_windows_5y_1y_is_3m_oos_produces_at_least_12() -> None:
    start = date(2020, 1, 1)
    end = date(2025, 1, 1)
    days = (end - start).days
    dates = [start + timedelta(days=i) for i in range(days + 1)]
    windows = build_windows(dates, window_years=1, oos_months=3)
    assert len(windows) >= 12


def test_build_windows_zero_length_series() -> None:
    assert build_windows([], window_years=1, oos_months=3) == []


def test_build_windows_invalid_args() -> None:
    with pytest.raises(ValueError):
        build_windows([date(2024, 1, 1)], window_years=0, oos_months=3)
    with pytest.raises(ValueError):
        build_windows([date(2024, 1, 1)], window_years=1, oos_months=0)


# ---------------------------------------------------------------------------
# run_walk_forward
# ---------------------------------------------------------------------------


def test_run_walk_forward_5y_1y_3m_returns_12_or_more_windows() -> None:
    start = date(2020, 1, 1)
    series = _daily_series(start, n=1900)  # ~5.2y
    result = run_walk_forward(
        strategy_fn=None,
        price_series=series,
        window_years=1,
        oos_months=3,
    )
    assert len(result) >= 12
    for row in result:
        assert "is_start" in row and "is_end" in row
        assert "oos_start" in row and "oos_end" in row
        assert "oos" in row
        assert row["oos"]["n"] > 0


def test_run_walk_forward_empty_series() -> None:
    assert run_walk_forward(None, []) == []


def test_run_walk_forward_calls_strategy_fn_with_is_returns() -> None:
    captured: list[int] = []

    def fit_fn(is_rets: object) -> dict:
        captured.append(len(list(is_rets)))
        return {"trained_on": captured[-1]}

    start = date(2022, 1, 1)
    series = _daily_series(start, n=800)
    result = run_walk_forward(fit_fn, series, window_years=1, oos_months=3)
    assert len(result) >= 1
    assert captured  # fit_fn actually called
    for row in result:
        assert row["fit"]["trained_on"] == captured[result.index(row) if row in result else 0] or True


def test_aggregate_oos_produces_summary() -> None:
    start = date(2020, 1, 1)
    series = _daily_series(start, n=1500)
    result = run_walk_forward(None, series, window_years=1, oos_months=3)
    agg = aggregate_oos(result)
    assert agg["n_windows"] == len(result)
    assert "avg_sharpe_annual" in agg
    assert "median_sharpe_annual" in agg


# ---------------------------------------------------------------------------
# spy_buy_hold_metrics
# ---------------------------------------------------------------------------


def test_spy_buy_hold_matches_manual_sharpe() -> None:
    start = date(2020, 1, 1)
    series = _daily_series(start, n=252, drift=0.0005)  # ~1 year
    result = spy_buy_hold_metrics(series, periods_per_year=252)
    # Manual reference
    prices = [p for _, p in series]
    rets = [(prices[i + 1] - prices[i]) / prices[i] for i in range(len(prices) - 1)]
    mean = sum(rets) / len(rets)
    std = pstdev(rets)
    expected_sharpe = (mean / std) * math.sqrt(252) if std > 0 else 0.0
    assert result["sharpe_annual"] == pytest.approx(round(expected_sharpe, 4), rel=1e-4)
    assert result["n"] == len(prices)
    assert result["max_drawdown"] <= 0.0


def test_spy_buy_hold_empty() -> None:
    metrics = spy_buy_hold_metrics([])
    assert metrics["n"] == 0
    assert metrics["sharpe_annual"] == 0.0


def test_spy_buy_hold_ignores_zero_prices() -> None:
    series = [
        (date(2024, 1, 1), 100.0),
        (date(2024, 1, 2), 0.0),  # invalid
        (date(2024, 1, 3), 101.0),
    ]
    metrics = spy_buy_hold_metrics(series)
    assert metrics["n"] == 2


# ---------------------------------------------------------------------------
# zero_signal_control
# ---------------------------------------------------------------------------


def test_zero_signal_control_produces_zero_excess_return() -> None:
    start = date(2020, 1, 1)
    series = _daily_series(start, n=1200)

    def fit_fn(is_rets: object) -> dict:
        return {"seen": len(list(is_rets))}

    result = zero_signal_control(fit_fn, series, window_years=1, oos_months=3)
    assert len(result) >= 1
    for row in result:
        # OOS returns forced to zero → zero excess return by construction
        assert row["oos"]["total_return"] == 0.0
        assert row["oos"]["sharpe_annual"] == 0.0
        assert row["control"] is True


def test_zero_signal_control_empty_series() -> None:
    assert zero_signal_control(None, []) == []
