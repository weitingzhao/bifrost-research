"""Pure helpers for candidate outcome settlement.

Kept separate from ``entry.py`` so the scoring rules are testable without a
database, matching ``engines/signal_hit/build.py``.
"""

from __future__ import annotations

DEFAULT_HORIZONS: tuple[int, ...] = (1, 5, 20)

# Candidates carry no direction, so "did it go up" mostly measures the market.
# SPY is the reference leg; excess return is what the funnel is judged on.
DEFAULT_BENCHMARK = "SPY"


def excess_hit(
    *,
    forward_return: float | None,
    benchmark_return: float | None,
) -> tuple[float | None, bool | None]:
    """Excess return over the benchmark, and whether it beat it.

    Returns ``(None, None)`` when either leg is missing — an unsettled horizon
    must not be recorded as a miss, which is what a ``False`` would mean once it
    reaches a hit-rate average.
    """
    if forward_return is None or benchmark_return is None:
        return None, None
    excess = float(forward_return) - float(benchmark_return)
    return excess, excess > 0.0
