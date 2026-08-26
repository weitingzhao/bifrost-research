"""SVI Vol Surface engine — Wave RS-B-Surface1.

Fits Gatheral raw-parameterization SVI to a smile of implied volatilities:

    w(k) = a + b*(rho*(k-m) + sqrt((k-m)^2 + sigma^2))

where ``w = sigma_IV^2 * T`` (total variance) and ``k = ln(K / F)`` (log-moneyness
relative to the forward). Persists parameters and per-strike residuals into
``features.option_surface_fit_daily`` and ``features.option_surface_residual_daily``.
"""

from bifrost_research.engines.vol_surface.svi import (
    svi_total_variance,
    svi_iv,
    svi_atm_slope,
    check_arbitrage_free,
)
from bifrost_research.engines.vol_surface.fit import (
    SviFitResult,
    fit_svi_smile,
)

__all__ = [
    "svi_total_variance",
    "svi_iv",
    "svi_atm_slope",
    "check_arbitrage_free",
    "SviFitResult",
    "fit_svi_smile",
]
