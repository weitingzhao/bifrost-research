"""Tests for Wave RS-B-Surface1 SVI engine."""

from __future__ import annotations

import math

import pytest

from bifrost_research.engines.vol_surface import (
    check_arbitrage_free,
    fit_svi_smile,
    svi_atm_slope,
    svi_iv,
    svi_total_variance,
)


def test_svi_total_variance_shape_matches_formula():
    a, b, rho, m, sigma = 0.04, 0.4, -0.3, 0.0, 0.1
    for k in (-0.2, -0.05, 0.0, 0.05, 0.2):
        expected = a + b * (rho * (k - m) + math.sqrt((k - m) ** 2 + sigma**2))
        assert math.isclose(svi_total_variance(k, a, b, rho, m, sigma), expected)


def test_svi_iv_positive_when_w_positive():
    T = 30 / 365
    iv = svi_iv(0.0, T, 0.04, 0.4, -0.3, 0.0, 0.1)
    assert iv > 0


def test_svi_iv_requires_positive_T():
    with pytest.raises(ValueError):
        svi_iv(0.0, 0.0, 0.04, 0.4, -0.3, 0.0, 0.1)


def test_svi_total_variance_requires_positive_sigma():
    with pytest.raises(ValueError):
        svi_total_variance(0.0, 0.04, 0.4, -0.3, 0.0, 0.0)


def test_svi_atm_slope_agrees_with_numeric_derivative():
    a, b, rho, m, sigma = 0.04, 0.4, -0.3, 0.01, 0.1
    h = 1e-6
    numeric = (
        svi_total_variance(h, a, b, rho, m, sigma)
        - svi_total_variance(-h, a, b, rho, m, sigma)
    ) / (2 * h)
    analytic = svi_atm_slope(a, b, rho, m, sigma)
    assert math.isclose(analytic, numeric, rel_tol=1e-4, abs_tol=1e-6)


def test_check_arbitrage_free_bound():
    T = 30 / 365
    # b=0.4, |rho|=0.3 → b*(1+|rho|) = 0.52; 4/T ≈ 48.7 → holds
    assert check_arbitrage_free(T, 0.4, -0.3) is True
    # Push b huge → violate
    assert check_arbitrage_free(T, 100.0, -0.9) is False
    with pytest.raises(ValueError):
        check_arbitrage_free(0.0, 0.4, -0.3)


def _synthetic_smile(
    true_params: tuple[float, float, float, float, float],
    T: float,
    ks: list[float],
) -> list[float]:
    a, b, rho, m, sigma = true_params
    ivs: list[float] = []
    for k in ks:
        w = svi_total_variance(k, a, b, rho, m, sigma)
        ivs.append(math.sqrt(max(w, 0.0) / T))
    return ivs


def test_fit_svi_recovers_smooth_smile_and_low_rmse():
    T = 30 / 365
    true_params = (0.04, 0.4, -0.3, 0.0, 0.1)
    ks = [round(-0.3 + 0.02 * i, 4) for i in range(31)]
    ivs = _synthetic_smile(true_params, T, ks)
    fit = fit_svi_smile(ks, ivs, T)
    assert fit is not None
    assert fit.n_points == len(ks)
    # RMSE on IV should be small on smooth synthetic smile
    assert fit.rmse < 0.01, f"rmse={fit.rmse}"
    # Arbitrage-free constraint holds on synthetic input
    assert fit.arb_free is True
    # ATM vol should recover: for raw SVI with m=0, w(0) = a + b*sigma
    a_t, b_t, _, _, sigma_t = true_params
    true_atm = math.sqrt((a_t + b_t * sigma_t) / T)
    assert math.isclose(fit.atm_vol(), true_atm, rel_tol=0.05, abs_tol=0.02)


def test_fit_svi_returns_none_when_too_few_points():
    T = 30 / 365
    ks = [-0.1, 0.0, 0.1]
    ivs = [0.3, 0.28, 0.32]
    assert fit_svi_smile(ks, ivs, T) is None


def test_fit_svi_returns_none_when_T_nonpositive():
    ks = [-0.1, -0.05, 0.0, 0.05, 0.1, 0.15]
    ivs = [0.3, 0.28, 0.27, 0.28, 0.29, 0.31]
    assert fit_svi_smile(ks, ivs, 0.0) is None
    assert fit_svi_smile(ks, ivs, -0.1) is None


def test_fit_svi_ignores_invalid_ivs():
    T = 30 / 365
    true_params = (0.04, 0.4, -0.3, 0.0, 0.1)
    ks = [round(-0.2 + 0.02 * i, 4) for i in range(21)]
    ivs = _synthetic_smile(true_params, T, ks)
    # Poison a few points
    ivs[3] = float("nan")
    ivs[7] = -1.0
    ivs[10] = 10.0
    fit = fit_svi_smile(ks, ivs, T)
    assert fit is not None
    # 18 valid points remain
    assert fit.n_points == len(ks) - 3
    assert fit.rmse < 0.02


def test_fit_svi_result_atm_vol_and_slope_finite():
    T = 45 / 365
    ks = [round(-0.25 + 0.025 * i, 4) for i in range(21)]
    ivs = _synthetic_smile((0.05, 0.5, -0.4, 0.01, 0.12), T, ks)
    fit = fit_svi_smile(ks, ivs, T)
    assert fit is not None
    assert math.isfinite(fit.atm_vol())
    assert math.isfinite(fit.atm_slope())
