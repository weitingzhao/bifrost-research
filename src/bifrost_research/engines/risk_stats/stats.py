"""Beta, correlation and the realised-vol cone — pure functions on close series.

The Loop could say a name's IV was rich and its regime negative-gamma but never
"how much of this move is the market" or "is 30-day realised vol high for this
name". These three read ``raw_market.stock_daily`` on request (RS2, Owner chose
compute-on-read over a nightly table).

Every estimate carries its own sample size, and a window that is not filled
returns None rather than a number computed from a shorter window — a beta on 30
of 252 sessions is not a 252-day beta.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

TRADING_DAYS = 252
# A window under this share of its nominal length is not that window.
MIN_FILL = 0.8


def log_returns(closes: Sequence[float]) -> list[float]:
    """Daily log returns; a non-positive or missing close breaks the chain, so it is skipped."""
    out: list[float] = []
    prev: float | None = None
    for c in closes:
        cur = float(c) if c is not None and float(c) > 0 else None
        if prev is not None and cur is not None:
            out.append(math.log(cur / prev))
        prev = cur
    return out


def aligned_returns(
    left: Mapping[Any, float],
    right: Mapping[Any, float],
) -> tuple[list[float], list[float], list[Any]]:
    """Log returns of two close series on the dates they share.

    Returns are built *after* the intersection, so a date one series is missing
    does not silently splice two non-adjacent closes into one return.
    """
    dates = sorted(set(left) & set(right))
    l_ret = log_returns([left[d] for d in dates])
    r_ret = log_returns([right[d] for d in dates])
    return l_ret, r_ret, dates[1:]


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def beta(asset: Sequence[float], benchmark: Sequence[float]) -> float | None:
    """cov(asset, benchmark) / var(benchmark) on paired returns."""
    n = min(len(asset), len(benchmark))
    if n < 2:
        return None
    a, b = list(asset[-n:]), list(benchmark[-n:])
    ma, mb = _mean(a), _mean(b)
    var = sum((x - mb) ** 2 for x in b)
    if var <= 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / var


def correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Pearson correlation on paired returns."""
    n = min(len(left), len(right))
    if n < 2:
        return None
    a, b = list(left[-n:]), list(right[-n:])
    ma, mb = _mean(a), _mean(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(va * vb)


def sufficient(n: int, window: int) -> bool:
    """Is a window filled enough to answer as that window (RS2: 80%)."""
    return n >= math.ceil(window * MIN_FILL)


def realised_vol(returns: Sequence[float]) -> float | None:
    """Annualised realised volatility of a return window (population sigma × √252)."""
    if len(returns) < 2:
        return None
    m = _mean(returns)
    var = sum((r - m) ** 2 for r in returns) / len(returns)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def rolling_realised_vol(returns: Sequence[float], window: int) -> list[float]:
    """Every complete ``window``-day realised vol in the series."""
    if window < 2 or len(returns) < window:
        return []
    return [
        rv
        for i in range(window, len(returns) + 1)
        if (rv := realised_vol(returns[i - window : i])) is not None
    ]


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation percentile, the same rule ``percentile_cont`` applies."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return xs[low]
    return xs[low] + (xs[high] - xs[low]) * (pos - low)


CONE_QUANTILES = ((0.05, "p05"), (0.20, "p20"), (0.50, "p50"), (0.80, "p80"), (0.95, "p95"))


def rv_cone(returns: Sequence[float], tenors: Iterable[int]) -> list[dict[str, Any]]:
    """Percentiles of each tenor's rolling realised vol over the lookback."""
    out: list[dict[str, Any]] = []
    for days in tenors:
        series = rolling_realised_vol(returns, days)
        row: dict[str, Any] = {"days": days, "n": len(series)}
        for q, key in CONE_QUANTILES:
            row[key] = percentile(series, q) if series else None
        out.append(row)
    return out


def current_realised_vol(returns: Sequence[float], days: int) -> float | None:
    """The latest complete ``days``-session realised vol — where the name sits in its cone."""
    if len(returns) < days:
        return None
    return realised_vol(returns[-days:])
