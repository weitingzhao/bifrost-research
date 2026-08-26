"""Walk-forward validation for backtests — Wave RS-C3.

The core primitive is a **rolling window** iterator over a time series of
returns / P&L observations. Each window has an in-sample (IS) fit slice and
an out-of-sample (OOS) evaluation slice. Callers pass a ``strategy_fn`` that
consumes the IS returns and produces a scalar (or dict) *fit* — the
walk-forward routine then evaluates the OOS returns and reports per-window
metrics.

D10 BLOCKED — pure evaluation, no order execution path.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Sequence


@dataclass
class Window:
    is_start: date
    is_end: date
    oos_start: date
    oos_end: date
    is_returns: list[float] = field(default_factory=list)
    oos_returns: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_start": self.is_start.isoformat(),
            "is_end": self.is_end.isoformat(),
            "oos_start": self.oos_start.isoformat(),
            "oos_end": self.oos_end.isoformat(),
            "is_n": len(self.is_returns),
            "oos_n": len(self.oos_returns),
        }


def _add_months(d: date, months: int) -> date:
    """Add months to ``d``, clamping the day to the target month length."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return (date(year + 1, 1, 1) - date(year, 12, 1)).days
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _summarize(returns: Sequence[float], *, periods_per_year: int = 252) -> dict[str, float]:
    if not returns:
        return {
            "n": 0,
            "mean": 0.0,
            "stdev": 0.0,
            "sharpe_annual": 0.0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
        }
    n = len(returns)
    mean = sum(returns) / n
    stdev = statistics.pstdev(returns) if n > 1 else 0.0
    sharpe = (mean / stdev) * math.sqrt(periods_per_year) if stdev > 0 else 0.0
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for r in returns:
        cum += r
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    wins = sum(1 for r in returns if r > 0)
    return {
        "n": n,
        "mean": round(mean, 8),
        "stdev": round(stdev, 8),
        "sharpe_annual": round(sharpe, 4),
        "total_return": round(cum, 6),
        "max_drawdown": round(max_dd, 6),
        "win_rate": round(wins / n, 4),
    }


def build_windows(
    dates: Sequence[date],
    *,
    window_years: int = 1,
    oos_months: int = 3,
) -> list[Window]:
    """Emit non-overlapping OOS windows across the date range.

    - ``window_years`` = length of the in-sample calibration slice
    - ``oos_months`` = length of the out-of-sample evaluation slice
    - IS windows roll forward by ``oos_months`` each iteration.
    """
    if not dates:
        return []
    if window_years < 1 or oos_months < 1:
        raise ValueError("window_years and oos_months must be >= 1")

    ordered = sorted(dates)
    first, last = ordered[0], ordered[-1]

    windows: list[Window] = []
    is_start = first
    while True:
        is_end = _add_months(is_start, 12 * window_years) - timedelta(days=1)
        oos_start = is_end + timedelta(days=1)
        oos_end = _add_months(oos_start, oos_months) - timedelta(days=1)
        if oos_end > last:
            break
        windows.append(
            Window(
                is_start=is_start,
                is_end=is_end,
                oos_start=oos_start,
                oos_end=oos_end,
            )
        )
        is_start = _add_months(is_start, oos_months)
    return windows


def run_walk_forward(
    strategy_fn: Callable[[Sequence[float]], Any] | None,
    price_series: Sequence[tuple[date, float]],
    *,
    window_years: int = 1,
    oos_months: int = 3,
    periods_per_year: int = 252,
) -> list[dict[str, Any]]:
    """Rolling walk-forward with IS/OOS split.

    ``price_series`` is a list of ``(date, price)`` observations (e.g. daily
    close). Returns are computed as simple day-over-day returns. Each window
    reports the OOS metrics dict + IS/OOS date bounds.

    ``strategy_fn`` may be ``None`` (buy-hold benchmark) or a callable that
    receives the IS returns and returns a dict — its shape is copied into
    the per-window ``fit`` field verbatim.
    """
    if not price_series:
        return []
    ordered = sorted(price_series, key=lambda pr: pr[0])
    if len(ordered) < 2:
        return []

    # Simple returns
    rets: list[tuple[date, float]] = []
    prev_price = ordered[0][1]
    for d, p in ordered[1:]:
        if prev_price and p is not None:
            rets.append((d, (p - prev_price) / prev_price))
        prev_price = p if p is not None else prev_price

    dates = [d for d, _ in rets]
    windows = build_windows(dates, window_years=window_years, oos_months=oos_months)

    out: list[dict[str, Any]] = []
    for w in windows:
        is_rets = [r for (d, r) in rets if w.is_start <= d <= w.is_end]
        oos_rets = [r for (d, r) in rets if w.oos_start <= d <= w.oos_end]
        fit: Any = None
        if strategy_fn is not None:
            try:
                fit = strategy_fn(is_rets)
            except Exception as exc:  # pragma: no cover - safety net
                fit = {"error": str(exc)}
        oos_metrics = _summarize(oos_rets, periods_per_year=periods_per_year)
        out.append(
            {
                **w.to_dict(),
                "fit": fit,
                "oos": oos_metrics,
            }
        )
    return out


def aggregate_oos(walk_forward_result: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-window OOS metrics into a single dict."""
    windows = list(walk_forward_result)
    if not windows:
        return {"n_windows": 0}
    sharpes = [w["oos"].get("sharpe_annual", 0.0) for w in windows]
    totals = [w["oos"].get("total_return", 0.0) for w in windows]
    wins = [w["oos"].get("win_rate", 0.0) for w in windows]
    return {
        "n_windows": len(windows),
        "avg_sharpe_annual": round(statistics.fmean(sharpes), 4) if sharpes else 0.0,
        "median_sharpe_annual": round(statistics.median(sharpes), 4) if sharpes else 0.0,
        "avg_total_return": round(statistics.fmean(totals), 6) if totals else 0.0,
        "avg_win_rate": round(statistics.fmean(wins), 4) if wins else 0.0,
    }


__all__ = [
    "Window",
    "build_windows",
    "run_walk_forward",
    "aggregate_oos",
]
