"""Benchmark comparisons for walk-forward backtests — Wave RS-C3.

Provides two comparators:

- ``spy_buy_hold_metrics(price_series)`` — Sharpe / total-return / max-DD of a
  simple long buy-hold on a price series (canonical SPY when available).
- ``zero_signal_control(strategy_fn, price_series, ...)`` — runs the same
  walk-forward machinery with the *signal strength forced to zero* — the
  strategy's `fit` step is still called, but the OOS returns are neutralized
  (all zeros), giving a null-hypothesis reference for whether the actual
  strategy adds value.

D10 BLOCKED — evaluation only.
"""

from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Any, Callable, Sequence

from bifrost_research.engines.backtest.walk_forward import (
    build_windows,
    run_walk_forward,
)


def spy_buy_hold_metrics(
    price_series: Sequence[tuple[date, float]],
    *,
    periods_per_year: int = 252,
) -> dict[str, Any]:
    """Sharpe / total-return / max-DD for a long buy-hold on ``price_series``."""
    if not price_series or len(price_series) < 2:
        return {
            "n": 0,
            "start_date": None,
            "end_date": None,
            "total_return": 0.0,
            "sharpe_annual": 0.0,
            "max_drawdown": 0.0,
            "cagr": 0.0,
        }
    ordered = sorted(price_series, key=lambda pr: pr[0])
    prices = [p for _, p in ordered if p is not None and p > 0]
    dates = [d for d, p in ordered if p is not None and p > 0]
    if len(prices) < 2:
        return {
            "n": 0,
            "start_date": None,
            "end_date": None,
            "total_return": 0.0,
            "sharpe_annual": 0.0,
            "max_drawdown": 0.0,
            "cagr": 0.0,
        }
    rets = [(prices[i + 1] - prices[i]) / prices[i] for i in range(len(prices) - 1)]
    mean = sum(rets) / len(rets)
    stdev = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (mean / stdev) * math.sqrt(periods_per_year) if stdev > 0 else 0.0
    total = (prices[-1] - prices[0]) / prices[0]
    # Max drawdown on cumulative returns.
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for r in rets:
        cum += r
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    days = max(1, (dates[-1] - dates[0]).days)
    years = days / 365.25
    cagr = (prices[-1] / prices[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    return {
        "n": len(prices),
        "start_date": dates[0].isoformat(),
        "end_date": dates[-1].isoformat(),
        "total_return": round(total, 6),
        "sharpe_annual": round(sharpe, 4),
        "max_drawdown": round(max_dd, 6),
        "cagr": round(cagr, 6),
    }


def zero_signal_control(
    strategy_fn: Callable[[Sequence[float]], Any] | None,
    price_series: Sequence[tuple[date, float]],
    *,
    window_years: int = 1,
    oos_months: int = 3,
    periods_per_year: int = 252,
) -> list[dict[str, Any]]:
    """Same walk-forward frame with OOS returns zeroed out.

    Emits per-window metrics where the OOS returns are exactly zero — i.e.
    the null-hypothesis benchmark of "the strategy adds no directional
    return". A strategy that beats this on OOS Sharpe / total return is
    doing better than chance.
    """
    if not price_series or len(price_series) < 2:
        return []
    ordered = sorted(price_series, key=lambda pr: pr[0])
    # Prepare fake series where OOS returns are zero. We accomplish this by
    # calling ``run_walk_forward`` on a synthetic price series that has zero
    # returns after the first IS slice — but the shared IS builder still needs
    # real prices to construct windows.
    dates_only = [d for d, _ in ordered]
    windows = build_windows(dates_only, window_years=window_years, oos_months=oos_months)
    if not windows:
        return []

    # Compute IS returns from the real series (so strategy_fn can be called),
    # then substitute zeros for the OOS series.
    flat_price_series: list[tuple[date, float]] = []
    for d, p in ordered:
        flat_price_series.append((d, p))
    # Run using real prices for IS metrics, then overwrite OOS metrics.
    real_result = run_walk_forward(
        strategy_fn=strategy_fn,
        price_series=flat_price_series,
        window_years=window_years,
        oos_months=oos_months,
        periods_per_year=periods_per_year,
    )
    for row in real_result:
        row["oos"] = {
            "n": row["oos"].get("n", 0),
            "mean": 0.0,
            "stdev": 0.0,
            "sharpe_annual": 0.0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
        }
        row["control"] = True
    return real_result


__all__ = [
    "spy_buy_hold_metrics",
    "zero_signal_control",
]
