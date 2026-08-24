"""Pure-compute tests for GEX engine (no DB)."""

from __future__ import annotations

from bifrost_research.engines.gex.exposure import (
    ContractGreeks,
    approx_bs_gamma,
    compute_gex_distribution,
    compute_gex_levels,
    gex_notional,
    strike_gex_from_contracts,
)


def test_approx_bs_gamma_positive() -> None:
    g = approx_bs_gamma(100.0, 100.0, iv=0.25, t_years=30 / 365)
    assert g > 0


def test_gex_sign_convention() -> None:
    spot = 100.0
    gamma = 0.05
    call = gex_notional(gamma, 1000, spot, sign=1.0)
    put = gex_notional(gamma, 1000, spot, sign=-1.0)
    assert call > 0
    assert put < 0
    assert abs(call + put) < 1e-9


def test_strike_distribution_and_walls() -> None:
    contracts = [
        ContractGreeks(strike=95.0, option_right="P", open_interest=5000, gamma=0.02),
        ContractGreeks(strike=100.0, option_right="C", open_interest=2000, gamma=0.04),
        ContractGreeks(strike=100.0, option_right="P", open_interest=2000, gamma=0.04),
        ContractGreeks(strike=105.0, option_right="C", open_interest=8000, gamma=0.03),
    ]
    dist, levels = compute_gex_distribution(contracts, spot=100.0)
    assert len(dist) == 3
    assert levels["major_call_wall"] == 105.0
    assert levels["major_put_wall"] == 95.0
    assert levels["zero_gamma"] is not None
    assert levels["total_net_gex"] == sum(r["net_gex"] for r in dist)


def test_volume_source_flag() -> None:
    contracts = [
        ContractGreeks(
            strike=100.0, option_right="C", open_interest=100, volume=50, gamma=0.02
        ),
    ]
    rows = strike_gex_from_contracts(contracts, 100.0)
    assert rows[0]["gex_source"].endswith("+volume")
    assert rows[0]["volume_net_gex"] != 0.0


def test_levels_empty() -> None:
    levels = compute_gex_levels([], 100.0)
    assert levels["total_net_gex"] == 0.0
    assert levels["zero_gamma"] is None
