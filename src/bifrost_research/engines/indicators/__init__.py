"""Standard technical indicators and their crossing signals, computed server-side (W6).

The chart used to compute these in the browser, where nothing could backtest
them. The math here mirrors ``bifrost-trade-frontend``'s ``barsChartMath.ts``
(SMA-seeded EMA, Wilder RSI, population-σ Bollinger) so the overlay does not
move when the chart switches to the server series.
"""

from bifrost_research.engines.indicators.compute import (
    SIGNALS,
    SignalSpec,
    bollinger,
    catalog,
    ema,
    get_signal,
    macd,
    rsi,
    signal_dates,
    signal_mask,
)

__all__ = [
    "SIGNALS",
    "SignalSpec",
    "bollinger",
    "catalog",
    "ema",
    "get_signal",
    "macd",
    "rsi",
    "signal_dates",
    "signal_mask",
]
