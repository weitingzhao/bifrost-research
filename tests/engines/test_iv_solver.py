"""Unit tests for Historical IV Brent solver (no DB required for core)."""

from __future__ import annotations

import pytest

from bifrost_research.engines.backtest.canonical_pnl import bs_price
from bifrost_research.engines.volatility.iv_solver import solve_iv


KNOWN_CASES = [
    # spot, strike, tte, right, known_iv
    (100.0, 100.0, 30 / 365.0, "C", 0.25),
    (100.0, 100.0, 45 / 365.0, "P", 0.30),
    (150.0, 145.0, 21 / 365.0, "C", 0.40),
    (80.0, 85.0, 60 / 365.0, "P", 0.22),
    (250.0, 260.0, 14 / 365.0, "C", 0.55),
]


@pytest.mark.parametrize("spot,strike,tte,right,known_iv", KNOWN_CASES)
def test_solve_iv_round_trip(spot, strike, tte, right, known_iv):
    mid = bs_price(spot, strike, tte, known_iv, right=right)
    iv, status = solve_iv(spot, strike, tte, mid, right)
    assert status == "ok"
    assert iv is not None
    assert abs(iv - known_iv) < 1e-3


def test_insufficient_when_mid_below_intrinsic():
    # Call intrinsic = 10, mid too low
    iv, status = solve_iv(110.0, 100.0, 30 / 365.0, 5.0, "C")
    assert status == "insufficient_inputs"
    assert iv is None


def test_insufficient_zero_spot():
    iv, status = solve_iv(0.0, 100.0, 0.1, 5.0, "C")
    assert status == "insufficient_inputs"


def test_polygon_style_atm_backcheck_tolerance():
    """Synthetic stand-in for Polygon ATM back-check: 20 nearby IVs."""
    rel_errs: list[float] = []
    for i in range(20):
        spot = 100.0 + i
        known = 0.18 + i * 0.01
        tte = (20 + i) / 365.0
        mid = bs_price(spot, spot, tte, known, right="C")
        iv, status = solve_iv(spot, spot, tte, mid, "C")
        assert status == "ok" and iv is not None
        rel_errs.append(abs(iv - known) / known)
    median = sorted(rel_errs)[len(rel_errs) // 2]
    assert median < 0.03
    assert max(rel_errs) < 0.05
