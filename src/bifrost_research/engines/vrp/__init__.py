"""VRP (Volatility Risk Premium) engine — Wave RS-B-VRP1.

Rolling Realized Volatility (RV) vs ATM IV → spread → percentile rank.

Reads:
    raw_market.stock_daily (close-to-close returns)
    features.option_metric_atm_iv_daily (30d ATM IV proxy)

Writes:
    features.stock_signal_vrp_daily
"""

from __future__ import annotations

from bifrost_research.engines.vrp.compute import (
    annualized_close_to_close_rv,
    compute_vrp_for_date,
    vrp_percentile_rank,
)

__all__ = [
    "annualized_close_to_close_rv",
    "compute_vrp_for_date",
    "vrp_percentile_rank",
]
