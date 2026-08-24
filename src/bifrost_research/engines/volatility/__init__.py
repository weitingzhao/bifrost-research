"""Volatility engines — Max Pain, ATM IV, PCR, IV Percentile (Wave 2) + Surface (Wave 3).

Ownership: Research OLAP domain writes ``features_daily.*`` and ``features_option.iv_surface_daily``.
"""

from __future__ import annotations

from bifrost_research.engines.volatility.atm_iv import compute_atm_iv_for_date
from bifrost_research.engines.volatility.iv_percentile import compute_iv_percentile_for_date
from bifrost_research.engines.volatility.max_pain import (
    compute_max_pain_curve,
    compute_max_pain_for_date,
    strike_map_for_expiry,
)
from bifrost_research.engines.volatility.pcr import compute_pcr_for_date
from bifrost_research.engines.volatility.surface import (
    fit_iv_surface,
    fit_polynomial_smile,
    fit_svi_smile,
    vol_cone_from_history,
)

__all__ = [
    "compute_atm_iv_for_date",
    "compute_iv_percentile_for_date",
    "compute_max_pain_curve",
    "compute_max_pain_for_date",
    "compute_pcr_for_date",
    "fit_iv_surface",
    "fit_polynomial_smile",
    "fit_svi_smile",
    "strike_map_for_expiry",
    "vol_cone_from_history",
]
