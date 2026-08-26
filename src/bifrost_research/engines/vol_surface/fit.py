"""SVI smile fitter — least-squares over ``w(k) = a + b*(rho*(k-m) + sqrt((k-m)^2 + sigma^2))``.

Prefers ``scipy.optimize.least_squares`` when available (better convergence on
5-parameter non-linear problems). Falls back to a pure-Python Nelder-Mead so
tests and dry-run environments without scipy still work.

Bounds follow Gatheral's raw SVI conventions:
    a ≥ 0, b ≥ 0, -1 < rho < 1, m ∈ [-1, 1], sigma > 0.

Additionally we validate ``b * (1 + |rho|) < 4 / T`` post-fit.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable, Sequence

from bifrost_research.engines.vol_surface.svi import (
    check_arbitrage_free,
    svi_atm_slope,
    svi_total_variance,
)

logger = logging.getLogger(__name__)

_EPS = 1e-9


@dataclass(frozen=True)
class SviFitResult:
    """SVI fit result.

    ``iv_market`` and ``iv_fitted`` are aligned to the input ``strikes``.
    """
    a: float
    b: float
    rho: float
    m: float
    sigma: float
    rmse: float
    n_points: int
    T: float
    log_moneyness: tuple[float, ...]
    iv_market: tuple[float, ...]
    iv_fitted: tuple[float, ...]
    arb_free: bool

    def atm_variance(self) -> float:
        return svi_total_variance(0.0, self.a, self.b, self.rho, self.m, self.sigma)

    def atm_vol(self) -> float:
        if self.T <= 0.0:
            return float("nan")
        return math.sqrt(max(self.atm_variance(), 0.0) / self.T)

    def atm_slope(self) -> float:
        return svi_atm_slope(self.a, self.b, self.rho, self.m, self.sigma)


def _residuals(
    params: Sequence[float],
    ks: Sequence[float],
    ws: Sequence[float],
) -> list[float]:
    a, b, rho, m, sigma = params
    out: list[float] = []
    for k, w_obs in zip(ks, ws):
        w_hat = svi_total_variance(k, a, b, rho, m, sigma)
        out.append(w_hat - w_obs)
    return out


def _sum_sq_residuals(
    params: Sequence[float],
    ks: Sequence[float],
    ws: Sequence[float],
) -> float:
    r = _residuals(params, ks, ws)
    return sum(v * v for v in r)


def _initial_guess(
    ks: Sequence[float],
    ws: Sequence[float],
) -> tuple[float, float, float, float, float]:
    """Cheap heuristic initializer using min-variance level and wing slope."""
    if not ws:
        return 0.0, 0.1, -0.2, 0.0, 0.1
    a0 = max(min(ws), 0.0) * 0.9
    b0 = max((max(ws) - min(ws)) / max(max(ks) - min(ks), 0.1), 0.05)
    rho0 = -0.2
    m0 = ks[ws.index(min(ws))] if min(ws) in ws else 0.0
    sigma0 = 0.1
    return a0, b0, rho0, m0, sigma0


def _clamp(params: Sequence[float]) -> tuple[float, float, float, float, float]:
    a, b, rho, m, sigma = (float(x) for x in params)
    a = max(a, 0.0)
    b = max(b, 0.0)
    rho = max(min(rho, 0.999), -0.999)
    m = max(min(m, 1.0), -1.0)
    sigma = max(sigma, _EPS)
    return a, b, rho, m, sigma


def _try_scipy_fit(
    ks: Sequence[float],
    ws: Sequence[float],
    p0: Sequence[float],
) -> tuple[float, ...] | None:
    try:  # pragma: no cover - scipy may not be installed in dev envs
        from scipy.optimize import least_squares  # type: ignore[import-not-found]
    except Exception:
        return None
    lb = [0.0, 0.0, -0.999, -1.0, _EPS]
    ub = [math.inf, math.inf, 0.999, 1.0, math.inf]
    p0_clamped = list(_clamp(p0))
    for i in range(5):
        if p0_clamped[i] < lb[i]:
            p0_clamped[i] = lb[i]
        if p0_clamped[i] > ub[i]:
            p0_clamped[i] = ub[i]
    try:
        result = least_squares(
            lambda params: _residuals(params, ks, ws),
            x0=p0_clamped,
            bounds=(lb, ub),
            method="trf",
            max_nfev=2000,
        )
    except Exception as exc:
        logger.info("scipy least_squares failed: %s", exc)
        return None
    if not result.success:
        logger.info("scipy least_squares did not converge: %s", result.message)
    return tuple(result.x)


def _nelder_mead(
    objective: Callable[[Sequence[float]], float],
    x0: Sequence[float],
    *,
    tol: float = 1e-8,
    max_iter: int = 2000,
) -> tuple[float, ...]:
    """Minimal Nelder-Mead. Sufficient for the 5-D SVI fit in absence of scipy."""
    n = len(x0)
    alpha, gamma_coef, rho_coef, sigma_coef = 1.0, 2.0, 0.5, 0.5
    simplex: list[list[float]] = [list(x0)]
    for i in range(n):
        pt = list(x0)
        step = 0.05 if pt[i] == 0 else 0.05 * abs(pt[i])
        pt[i] = pt[i] + max(step, 0.02)
        simplex.append(pt)

    def clamp_and_eval(x: Sequence[float]) -> tuple[list[float], float]:
        clamped = list(_clamp(x))
        return clamped, objective(clamped)

    scored = [clamp_and_eval(p) for p in simplex]
    for _ in range(max_iter):
        scored.sort(key=lambda kv: kv[1])
        best = scored[0][1]
        worst = scored[-1][1]
        if worst - best < tol:
            break
        centroid = [
            sum(scored[i][0][j] for i in range(n)) / n for j in range(n)
        ]
        xr = [centroid[j] + alpha * (centroid[j] - scored[-1][0][j]) for j in range(n)]
        xr_pt, fr = clamp_and_eval(xr)
        if scored[0][1] <= fr < scored[-2][1]:
            scored[-1] = (xr_pt, fr)
            continue
        if fr < scored[0][1]:
            xe = [centroid[j] + gamma_coef * (xr_pt[j] - centroid[j]) for j in range(n)]
            xe_pt, fe = clamp_and_eval(xe)
            scored[-1] = (xe_pt, fe) if fe < fr else (xr_pt, fr)
            continue
        xc = [centroid[j] + rho_coef * (scored[-1][0][j] - centroid[j]) for j in range(n)]
        xc_pt, fc = clamp_and_eval(xc)
        if fc < scored[-1][1]:
            scored[-1] = (xc_pt, fc)
            continue
        base = scored[0][0]
        new_simplex: list[tuple[list[float], float]] = [scored[0]]
        for i in range(1, len(scored)):
            shrunk = [base[j] + sigma_coef * (scored[i][0][j] - base[j]) for j in range(n)]
            new_simplex.append(clamp_and_eval(shrunk))
        scored = new_simplex
    scored.sort(key=lambda kv: kv[1])
    return tuple(scored[0][0])


def fit_svi_smile(
    log_moneyness: Sequence[float],
    iv_market: Sequence[float],
    T: float,
    *,
    initial_guess: Sequence[float] | None = None,
) -> SviFitResult | None:
    """Fit raw SVI to (k, iv_market) at time-to-expiry ``T`` (years).

    Returns None when the input is degenerate (not enough finite points).
    """
    if T <= 0.0:
        return None
    ks_all = list(log_moneyness)
    ivs_all = list(iv_market)
    if len(ks_all) != len(ivs_all):
        raise ValueError("log_moneyness and iv_market length mismatch")

    ks: list[float] = []
    ws: list[float] = []
    ivs: list[float] = []
    for k, iv in zip(ks_all, ivs_all):
        try:
            k_f = float(k)
            iv_f = float(iv)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(k_f) or not math.isfinite(iv_f):
            continue
        if iv_f <= 0.0 or iv_f > 5.0:
            continue
        ks.append(k_f)
        ivs.append(iv_f)
        ws.append(iv_f * iv_f * T)

    if len(ks) < 5:
        return None

    order = sorted(range(len(ks)), key=lambda i: ks[i])
    ks = [ks[i] for i in order]
    ivs = [ivs[i] for i in order]
    ws = [ws[i] for i in order]

    p0 = list(initial_guess) if initial_guess is not None else list(_initial_guess(ks, ws))
    scipy_params = _try_scipy_fit(ks, ws, p0)
    if scipy_params is not None:
        params = scipy_params
    else:
        params = _nelder_mead(
            lambda p: _sum_sq_residuals(p, ks, ws),
            p0,
        )
    a, b, rho, m, sigma = _clamp(params)

    fitted_iv: list[float] = []
    for k in ks:
        w_hat = svi_total_variance(k, a, b, rho, m, sigma)
        fitted_iv.append(math.sqrt(max(w_hat, 0.0) / T))
    resid = [fitted_iv[i] - ivs[i] for i in range(len(ivs))]
    mse = sum(r * r for r in resid) / len(resid)
    rmse = math.sqrt(max(mse, 0.0))

    return SviFitResult(
        a=a,
        b=b,
        rho=rho,
        m=m,
        sigma=sigma,
        rmse=round(rmse, 8),
        n_points=len(ks),
        T=T,
        log_moneyness=tuple(round(k, 8) for k in ks),
        iv_market=tuple(round(v, 8) for v in ivs),
        iv_fitted=tuple(round(v, 8) for v in fitted_iv),
        arb_free=bool(check_arbitrage_free(T, b, rho)),
    )
