"""Unit tests for OpEx cycle engine — Wave RS-B-OpEx1.

Covers:
* US monthly OpEx calendar (third Friday, next OpEx, is_opex_week).
* Analytical Vanna and Charm match numerical difference quotients of the
  Black-Scholes price on a synthetic contract (1e-4 tolerance for finite
  differences on a small step).
* Per-strike dealer aggregation applies the correct sign convention.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from bifrost_research.engines.opex_cycle.calendar import (
    days_to_opex,
    is_opex_week,
    next_opex_friday,
    third_friday,
)
from bifrost_research.engines.opex_cycle.vanna_charm import (
    ContractGreek,
    _norm_cdf,
    _norm_pdf,
    bs_charm,
    bs_vanna,
    strike_vanna_charm_from_contracts,
    zero_crossing_strike,
)


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------


def test_third_friday_september_2026() -> None:
    assert third_friday(2026, 9) == date(2026, 9, 18)


def test_third_friday_january_2026_edge() -> None:
    # Jan 2, 2026 is a Friday → third Friday should be Jan 16, 2026
    assert third_friday(2026, 1) == date(2026, 1, 16)


def test_third_friday_all_months_2026() -> None:
    # Every month must produce a valid Friday in that month
    for m in range(1, 13):
        d = third_friday(2026, m)
        assert d.weekday() == 4  # Friday
        assert d.month == m


def test_third_friday_invalid_month() -> None:
    with pytest.raises(ValueError):
        third_friday(2026, 13)


def test_next_opex_friday_regular_case() -> None:
    # Per Owner spec: from 2026-08-25 the next monthly OpEx is 2026-09-18
    assert next_opex_friday(date(2026, 8, 25)) == date(2026, 9, 18)


def test_next_opex_friday_before_this_month_third() -> None:
    # Aug 1 (Sat) — third Friday of Aug 2026 is Aug 21
    assert next_opex_friday(date(2026, 8, 1)) == date(2026, 8, 21)


def test_next_opex_friday_rolls_after_third() -> None:
    # If today equals the third Friday, roll to next month's OpEx
    assert next_opex_friday(date(2026, 8, 21)) == date(2026, 9, 18)


def test_next_opex_friday_year_rollover() -> None:
    # Dec 2026 third Friday is 2026-12-18 → the day after rolls into Jan 2027
    assert next_opex_friday(date(2026, 12, 19)) == date(2027, 1, 15)


def test_days_to_opex_forward() -> None:
    d = days_to_opex(date(2026, 8, 25))  # → 2026-09-18
    assert d == (date(2026, 9, 18) - date(2026, 8, 25)).days


def test_is_opex_week_on_friday() -> None:
    assert is_opex_week(date(2026, 8, 21)) is True


def test_is_opex_week_wednesday_of_opex_week() -> None:
    # Aug 19, 2026 is a Wednesday in the Aug OpEx week (Fri = Aug 21)
    assert is_opex_week(date(2026, 8, 19)) is True


def test_is_opex_week_off_week() -> None:
    # Aug 28, 2026 is a Friday one week after OpEx
    assert is_opex_week(date(2026, 8, 28)) is False


# --------------------------------------------------------------------------
# Vanna / Charm numerical validation
# --------------------------------------------------------------------------


def _bs_call_price(
    spot: float, strike: float, sigma: float, t: float, r: float = 0.0, q: float = 0.0
) -> float:
    if sigma <= 0 or t <= 0:
        return max(0.0, spot - strike)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return spot * math.exp(-q * t) * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)


def _bs_put_price(
    spot: float, strike: float, sigma: float, t: float, r: float = 0.0, q: float = 0.0
) -> float:
    if sigma <= 0 or t <= 0:
        return max(0.0, strike - spot)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * math.exp(-q * t) * _norm_cdf(-d1)


def _numerical_vanna_call(
    spot: float, strike: float, sigma: float, t: float, *, h_s: float = 1e-3, h_v: float = 1e-3
) -> float:
    # ∂²V/∂S∂σ ≈ (V(S+, σ+) − V(S+, σ−) − V(S−, σ+) + V(S−, σ−)) / (4·h_s·h_v)
    v_pp = _bs_call_price(spot + h_s, strike, sigma + h_v, t)
    v_pm = _bs_call_price(spot + h_s, strike, sigma - h_v, t)
    v_mp = _bs_call_price(spot - h_s, strike, sigma + h_v, t)
    v_mm = _bs_call_price(spot - h_s, strike, sigma - h_v, t)
    return (v_pp - v_pm - v_mp + v_mm) / (4.0 * h_s * h_v)


def _numerical_charm_call(
    spot: float, strike: float, sigma: float, t: float, *, h_t: float = 1e-5, h_s: float = 1e-3
) -> float:
    """Numerical ∂Δ/∂t (calendar time convention).

    Note: BS price is a function of time-to-expiry T. Calendar time t
    advances **against** T (as calendar time moves forward, T decreases),
    so ∂Δ/∂t = −∂Δ/∂T. The finite-difference below computes ∂Δ/∂T and
    negates the result to obtain the calendar-time convention that the
    analytical ``bs_charm`` returns.
    """

    def delta(tt: float) -> float:
        vp = _bs_call_price(spot + h_s, strike, sigma, tt)
        vm = _bs_call_price(spot - h_s, strike, sigma, tt)
        return (vp - vm) / (2.0 * h_s)

    d_delta_d_T = (delta(t + h_t) - delta(t - h_t)) / (2.0 * h_t)
    return -d_delta_d_T


def test_bs_vanna_matches_numerical_call_atm() -> None:
    spot, strike, sigma, t = 100.0, 100.0, 0.25, 30.0 / 365.0
    analytical = bs_vanna(spot, strike, sigma, t, option_right="C")
    numerical = _numerical_vanna_call(spot, strike, sigma, t)
    assert math.isclose(analytical, numerical, rel_tol=1e-3, abs_tol=1e-4)


def test_bs_vanna_matches_numerical_call_otm() -> None:
    spot, strike, sigma, t = 100.0, 110.0, 0.30, 60.0 / 365.0
    analytical = bs_vanna(spot, strike, sigma, t, option_right="C")
    numerical = _numerical_vanna_call(spot, strike, sigma, t)
    assert math.isclose(analytical, numerical, rel_tol=5e-3, abs_tol=1e-4)


def test_bs_vanna_same_for_calls_and_puts() -> None:
    # Vanna formula is identical for calls and puts
    spot, strike, sigma, t = 100.0, 95.0, 0.28, 45.0 / 365.0
    call = bs_vanna(spot, strike, sigma, t, option_right="C")
    put = bs_vanna(spot, strike, sigma, t, option_right="P")
    assert math.isclose(call, put, abs_tol=1e-12)


def test_bs_charm_matches_numerical_call_atm() -> None:
    spot, strike, sigma, t = 100.0, 100.0, 0.25, 30.0 / 365.0
    analytical = bs_charm(spot, strike, sigma, t, option_right="C")
    numerical = _numerical_charm_call(spot, strike, sigma, t)
    # Charm has larger numerical error; accept 1% tolerance on ATM
    assert math.isclose(analytical, numerical, rel_tol=1e-2, abs_tol=1e-3)


def test_bs_greeks_return_zero_for_bad_inputs() -> None:
    for bad_t in (-1.0, 0.0):
        assert bs_vanna(100.0, 100.0, 0.25, bad_t) == 0.0
        assert bs_charm(100.0, 100.0, 0.25, bad_t) == 0.0
    for bad_sigma in (-0.1, 0.0):
        assert bs_vanna(100.0, 100.0, bad_sigma, 0.1) == 0.0
        assert bs_charm(100.0, 100.0, bad_sigma, 0.1) == 0.0
    for bad_spot in (-1.0, 0.0):
        assert bs_vanna(bad_spot, 100.0, 0.25, 0.1) == 0.0
        assert bs_charm(bad_spot, 100.0, 0.25, 0.1) == 0.0


# --------------------------------------------------------------------------
# Dealer aggregation
# --------------------------------------------------------------------------


def _c(right: str, strike: float, oi: int = 100, iv: float = 0.25, t: float = 30.0 / 365.0) -> ContractGreek:
    return ContractGreek(strike=strike, option_right=right, open_interest=oi, iv=iv, t_years=t)


def test_strike_vanna_charm_dealer_sign() -> None:
    """Dealer aggregation must apply sign = −1 for calls, +1 for puts.

    Given identical (strike, iv, t) inputs, the *only* difference between the
    call and put aggregated rows is the dealer sign (BS vanna is identical
    for calls and puts). So per-contract signed contributions must be
    equal-and-opposite.
    """
    spot = 100.0
    strike = 100.0
    from bifrost_research.engines.gex.exposure import MULTIPLIER as MULT

    raw_vanna = bs_vanna(spot, strike, 0.25, 30.0 / 365.0, option_right="C")
    expected_call = -1.0 * raw_vanna * 100 * MULT  # dealer short calls
    expected_put = 1.0 * raw_vanna * 100 * MULT  # dealer long puts

    calls = strike_vanna_charm_from_contracts([_c("C", strike)], spot)
    puts = strike_vanna_charm_from_contracts([_c("P", strike)], spot)
    assert len(calls) == 1
    assert len(puts) == 1
    assert math.isclose(calls[0]["call_vanna"], expected_call, rel_tol=1e-6)
    assert math.isclose(puts[0]["put_vanna"], expected_put, rel_tol=1e-6)
    # Equal-and-opposite dealer contributions cancel
    assert math.isclose(
        calls[0]["call_vanna"], -puts[0]["put_vanna"], rel_tol=1e-6
    )


def test_strike_vanna_charm_aggregates_by_strike() -> None:
    spot = 100.0
    contracts = [
        _c("C", 100.0, oi=10),
        _c("C", 100.0, oi=20),
        _c("P", 100.0, oi=30),
    ]
    rows = strike_vanna_charm_from_contracts(contracts, spot)
    assert len(rows) == 1
    row = rows[0]
    assert row["call_oi"] == 30
    assert row["put_oi"] == 30
    # Both call sums should equal the sum of individual contract contributions
    single_call = strike_vanna_charm_from_contracts([_c("C", 100.0, oi=30)], spot)[0]
    assert math.isclose(row["call_vanna"], single_call["call_vanna"], rel_tol=1e-6)
    assert math.isclose(row["call_charm"], single_call["call_charm"], rel_tol=1e-6)


def test_strike_vanna_charm_skips_invalid_right() -> None:
    spot = 100.0
    rows = strike_vanna_charm_from_contracts(
        [
            ContractGreek(strike=100.0, option_right="X", open_interest=10, iv=0.25, t_years=0.1),
            _c("C", 100.0),
        ],
        spot,
    )
    # Only the valid call survives
    assert len(rows) == 1
    assert rows[0]["call_oi"] == 100


def test_zero_crossing_strike_linear_interp() -> None:
    dist = [
        {"strike": 90.0, "metric": -2.0},
        {"strike": 100.0, "metric": -1.0},
        {"strike": 110.0, "metric": 3.0},  # cumulative flips sign here
    ]
    # cumulative = [-2, -3, 0] → crosses zero between 100 and 110
    zero = zero_crossing_strike(dist, "metric", spot=100.0)
    assert zero is not None
    assert 100.0 <= zero <= 110.0


def test_zero_crossing_returns_none_when_never_crosses() -> None:
    dist = [
        {"strike": 90.0, "metric": 1.0},
        {"strike": 100.0, "metric": 2.0},
        {"strike": 110.0, "metric": 3.0},
    ]
    assert zero_crossing_strike(dist, "metric", spot=100.0) is None


def test_norm_pdf_and_cdf_reference_values() -> None:
    # φ(0) = 1/√(2π) ≈ 0.3989
    assert math.isclose(_norm_pdf(0.0), 1.0 / math.sqrt(2.0 * math.pi), rel_tol=1e-6)
    # N(0) = 0.5
    assert math.isclose(_norm_cdf(0.0), 0.5, rel_tol=1e-6)
    # N(1.96) ≈ 0.975
    assert math.isclose(_norm_cdf(1.96), 0.9750, abs_tol=1e-3)
