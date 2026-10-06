"""Black–Scholes for research — the one copy (TD-110).

European, no dividends. Every function takes rate (continuously compounded,
decimal) as a required keyword, so a silent r = 0 default cannot come back:
until 0.189.0 the stored IV features solved at r = 0 while the backtester used
the 1-month Treasury, and the same contract had two IVs and two deltas.

The convention is the Treasury curve from pricing.rates everywhere a rate
matters (IV inversion, delta, option prices). On 19,435 stored Brent rows
(2026-06-24..09-25) it moves call IV by -0.74 vol pts and put IV by +0.94
(median), and closes the near-ATM call-put gap from 2.37 to 0.20 pts — what
put-call parity says it should be. The call+put ATM average moves +0.07 pts.

rate=0.0 remains a legitimate argument where r does not move the answer to
first order (GEX gamma, an ATM straddle); the caller says so where it passes it.
"""

from __future__ import annotations

import math
from typing import Literal

SolverStatus = Literal[
    "ok",
    "no_convergence",
    "insufficient_inputs",
    "vendor_snapshot",
]

IV_LO = 0.01
IV_HI = 5.0
BRENT_TOL = 1e-4
BRENT_MAXITER = 100

_SQRT_2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _d1(spot: float, strike: float, t_years: float, iv: float, rate: float, q: float = 0.0) -> float:
    return (math.log(spot / strike) + (rate - q + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))


def _intrinsic(spot: float, strike: float, right: str) -> float:
    return max(0.0, (spot - strike) if right == "C" else (strike - spot))


def bs_price(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    *,
    right: Literal["C", "P"],
    rate: float,
) -> float:
    """Black–Scholes price. iv is decimal (0.25 = 25%)."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 1e-8:
        return _intrinsic(spot, strike, right)
    d1 = _d1(spot, strike, t_years, iv, rate)
    d2 = d1 - iv * math.sqrt(t_years)
    disc = math.exp(-rate * t_years)
    if right == "C":
        return spot * norm_cdf(d1) - strike * disc * norm_cdf(d2)
    return strike * disc * norm_cdf(-d2) - spot * norm_cdf(-d1)


def bs_delta(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    *,
    right: Literal["C", "P"],
    rate: float,
) -> float:
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 1e-8:
        if right == "C":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    d1 = _d1(spot, strike, t_years, iv, rate)
    if right == "C":
        return float(norm_cdf(d1))
    return float(norm_cdf(d1) - 1.0)


def bs_gamma(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    *,
    rate: float,
) -> float:
    """Gamma per unit of spot (same for calls and puts)."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 1e-8:
        return 0.0
    d1 = _d1(spot, strike, t_years, iv, rate)
    return norm_pdf(d1) / (spot * iv * math.sqrt(t_years))


def bs_vanna(
    spot: float,
    strike: float,
    sigma: float,
    t_years: float,
    *,
    rate: float,
    q: float = 0.0,
    option_right: str = "C",
) -> float:
    """∂²V/∂S∂σ = -exp(-qT)·φ(d1)·d2/σ, the same for calls and puts (Hull / Wilmott).

    ``option_right`` is accepted for symmetry with ``bs_charm`` and ignored.
    """
    _ = option_right
    if spot <= 0 or strike <= 0 or sigma <= 0 or t_years <= 0:
        return 0.0
    d1 = _d1(spot, strike, t_years, sigma, rate, q)
    d2 = d1 - sigma * math.sqrt(t_years)
    return -math.exp(-q * t_years) * norm_pdf(d1) * d2 / sigma


def bs_charm(
    spot: float,
    strike: float,
    sigma: float,
    t_years: float,
    *,
    rate: float,
    q: float = 0.0,
    option_right: str = "C",
) -> float:
    """∂Delta/∂t (positive-theta convention, per year)."""
    if spot <= 0 or strike <= 0 or sigma <= 0 or t_years <= 0:
        return 0.0
    sqrt_t = math.sqrt(t_years)
    d1 = _d1(spot, strike, t_years, sigma, rate, q)
    d2 = d1 - sigma * sqrt_t
    common = math.exp(-q * t_years) * norm_pdf(d1) * (
        2.0 * (rate - q) * t_years - d2 * sigma * sqrt_t
    ) / (2.0 * t_years * sigma * sqrt_t)
    right = (option_right or "C").strip().upper()
    if right in ("C", "CALL"):
        return -q * math.exp(-q * t_years) * norm_cdf(d1) - common
    return q * math.exp(-q * t_years) * norm_cdf(-d1) - common


def solve_iv(
    spot: float,
    strike: float,
    tte_years: float,
    mid: float,
    right: Literal["C", "P"],
    *,
    rate: float,
) -> tuple[float | None, SolverStatus]:
    """Invert Black–Scholes mid → IV via Brent (scipy-free). Returns (iv, status).

    rate is the continuously compounded risk-free rate as a decimal and has no
    default: pass risk_free_rate / RateCurve.on_or_before (TD-110).
    """
    if (
        spot <= 0
        or strike <= 0
        or tte_years <= 1e-8
        or mid is None
        or mid <= 0
    ):
        return None, "insufficient_inputs"

    # Intrinsic floor — a mid below intrinsic has no Black–Scholes volatility.
    intrinsic = max(0.0, (spot - strike) if right == "C" else (strike - spot))
    if mid < intrinsic * 0.999:
        return None, "insufficient_inputs"

    def objective(sigma: float) -> float:
        return bs_price(spot, strike, tte_years, sigma, right=right, rate=rate) - mid

    a, b = IV_LO, IV_HI
    fa, fb = objective(a), objective(b)
    # Expand upper bracket if needed (deep OTM / high premium)
    expand = 0
    while fa * fb > 0 and expand < 8:
        b *= 1.5
        if b > 10.0:
            break
        fb = objective(b)
        expand += 1
    if fa * fb > 0:
        return None, "no_convergence"

    # Brent (simplified: scipy-free)
    c, fc = a, fa
    d = e = b - a
    for _ in range(BRENT_MAXITER):
        if fb * fc > 0:
            c, fc = a, fa
            d = e = b - a
        if abs(fc) < abs(fb):
            a, b, c = b, c, b
            fa, fb, fc = fb, fc, fb
        tol1 = 2.0 * BRENT_TOL * abs(b) + 0.5 * BRENT_TOL
        xm = 0.5 * (c - b)
        if abs(xm) <= tol1 or abs(fb) <= BRENT_TOL * max(1.0, abs(mid)):
            if IV_LO <= b <= IV_HI * 2:
                return float(b), "ok"
            return None, "no_convergence"
        if abs(e) >= tol1 and abs(fa) > abs(fb):
            s = fb / fa
            if a == c:
                p = 2.0 * xm * s
                q = 1.0 - s
            else:
                q = fa / fc
                r = fb / fc
                p = s * (2.0 * xm * q * (q - r) - (b - a) * (r - 1.0))
                q = (q - 1.0) * (r - 1.0) * (s - 1.0)
            if p > 0:
                q = -q
            p = abs(p)
            min1 = 3.0 * xm * q - abs(tol1 * q)
            min2 = abs(e * q)
            if 2.0 * p < min(min1, min2):
                e = d
                d = p / q
            else:
                d = e = xm
        else:
            d = e = xm
        a, fa = b, fb
        if abs(d) > tol1:
            b += d
        else:
            b += math.copysign(tol1, xm)
        fb = objective(b)
    return None, "no_convergence"
