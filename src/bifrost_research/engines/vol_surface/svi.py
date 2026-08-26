"""Pure-Python Gatheral raw SVI (Stochastic Volatility Inspired) formulas.

Reference: Gatheral, J. (2004). "A parsimonious arbitrage-free implied volatility
parameterization with application to the valuation of volatility derivatives."

Raw parameterization of total variance ``w = sigma_IV^2 * T`` as a function of
log-moneyness ``k = ln(K/F)``:

    w(k) = a + b * (rho * (k - m) + sqrt((k - m)^2 + sigma^2))

Parameters:
    a     ≥ 0        vertical shift (min total variance level)
    b     ≥ 0        slope of the wings
    rho   ∈ (-1, 1)  wing asymmetry
    m     ∈ ℝ        smile centering
    sigma > 0        smoothness (ATM curvature)

Arbitrage-free necessary condition (Gatheral): ``b * (1 + |rho|) < 4 / T``.
This bounds calendar / butterfly spread arb; full arb-freeness needs additional
checks but this bound is the standard fit-time constraint.
"""

from __future__ import annotations

import math


def svi_total_variance(
    k: float,
    a: float,
    b: float,
    rho: float,
    m: float,
    sigma: float,
) -> float:
    """Evaluate SVI total variance ``w(k)`` at log-moneyness ``k``.

    Returns non-negative ``w``. Callers must ensure ``T > 0`` to convert to IV.
    """
    if sigma <= 0.0:
        raise ValueError("sigma must be > 0")
    dk = k - m
    return a + b * (rho * dk + math.sqrt(dk * dk + sigma * sigma))


def svi_iv(
    k: float,
    T: float,
    a: float,
    b: float,
    rho: float,
    m: float,
    sigma: float,
) -> float:
    """Convert SVI total variance to implied vol: ``sigma_IV = sqrt(w / T)``.

    Clamps ``w`` at 0 for numerical safety (SVI raw is not strictly ≥ 0 across
    all params; a well-fit surface stays non-negative in the observed range).
    """
    if T <= 0.0:
        raise ValueError("T must be > 0")
    w = svi_total_variance(k, a, b, rho, m, sigma)
    if not math.isfinite(w):
        return float("nan")
    return math.sqrt(max(w, 0.0) / T)


def svi_atm_slope(
    a: float,
    b: float,
    rho: float,
    m: float,
    sigma: float,
) -> float:
    """∂w/∂k evaluated at k=0 (ATM skew of total variance, not σ).

    dw/dk = b * (rho + (k - m) / sqrt((k - m)^2 + sigma^2))

    Evaluated at k = 0.
    """
    if sigma <= 0.0:
        raise ValueError("sigma must be > 0")
    dk = -m
    denom = math.sqrt(dk * dk + sigma * sigma)
    return b * (rho + dk / denom)


def check_arbitrage_free(
    T: float,
    b: float,
    rho: float,
) -> bool:
    """Necessary Gatheral arb-free bound: b * (1 + |rho|) < 4 / T.

    Returns True when the bound holds. Callers can use this in tests /
    post-fit validation.
    """
    if T <= 0.0:
        raise ValueError("T must be > 0")
    return b * (1.0 + abs(rho)) < 4.0 / T
