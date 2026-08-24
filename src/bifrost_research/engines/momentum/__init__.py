"""Momentum Radar engine — multi-timeframe momentum scoring (Wave 3.1).

Writes ``features.stock_signal_momentum_daily``.
"""

from __future__ import annotations

from bifrost_research.engines.momentum.radar import (
    FACTOR_NOTES,
    grade_from_score,
    path_from_factors,
    score_momentum,
)

__all__ = [
    "FACTOR_NOTES",
    "grade_from_score",
    "path_from_factors",
    "score_momentum",
]
