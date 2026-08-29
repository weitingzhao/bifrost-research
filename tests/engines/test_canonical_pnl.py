"""Unit tests for canonical structure PnL pricing (no DB / IB)."""

from __future__ import annotations

from datetime import date, timedelta

from bifrost_research.engines.backtest.canonical_pnl import (
    STRUCTURES,
    bs_delta,
    bs_price,
    build_entry_legs,
    default_params,
    mark_structure,
    net_entry_credit,
    simulate_trajectory,
    strike_for_delta,
)


def test_bs_call_put_parity_rough():
    spot, k, t, iv = 100.0, 100.0, 30 / 365.0, 0.25
    c = bs_price(spot, k, t, iv, right="C")
    p = bs_price(spot, k, t, iv, right="P")
    assert c > 0 and p > 0
    # put-call parity at r=0: C - P ≈ S - K
    assert abs((c - p) - (spot - k)) < 0.5


def test_strike_for_call_delta_otm():
    spot, t, iv = 100.0, 45 / 365.0, 0.30
    k = strike_for_delta(spot, t, iv, 0.15, right="C")
    d = bs_delta(spot, k, t, iv, right="C")
    assert k > spot
    assert abs(d - 0.15) < 0.03


def test_short_strangle_entry_credit_positive():
    entry = date(2026, 1, 15)
    legs, sp, q = build_entry_legs(
        "short_strangle", spot=100.0, atm_iv=0.25, entry_date=entry
    )
    credit = net_entry_credit(legs)
    assert credit > 0
    assert len(legs) == 2
    assert q in ("ok", "iv_interpolated")
    assert sp.params_hash()


def test_long_straddle_entry_debit():
    entry = date(2026, 1, 15)
    legs, _, _ = build_entry_legs(
        "long_straddle", spot=100.0, atm_iv=0.25, entry_date=entry
    )
    assert net_entry_credit(legs) < 0


def test_mark_pnl_near_zero_at_entry():
    entry = date(2026, 1, 15)
    legs, sp, q = build_entry_legs(
        "short_put", spot=100.0, atm_iv=0.22, entry_date=entry
    )
    entry_mid = net_entry_credit(legs)
    m = mark_structure(
        legs,
        structure="short_put",
        params=sp,
        entry_date=entry,
        as_of_date=entry,
        entry_spot=100.0,
        entry_atm_iv=0.22,
        entry_mid=entry_mid,
        as_of_spot=100.0,
        as_of_atm_iv=0.22,
        data_quality=q,
    )
    assert m.pnl_since_entry is not None
    assert abs(m.pnl_since_entry) < 5.0  # pennies of BS / rounding


def test_simulate_trajectory_spot_up_hurts_short_put():
    entry = date(2026, 1, 2)
    days = [entry + timedelta(days=i) for i in range(0, 21, 5)]
    spots = {d: 100.0 + i * 2 for i, d in enumerate(days)}
    ivs = {d: 0.25 for d in days}
    marks = simulate_trajectory(
        "short_put",
        entry_date=entry,
        as_of_dates=days,
        spots=spots,
        atm_ivs=ivs,
    )
    assert len(marks) == len(days)
    # spot up → short put should improve (or stay non-worse than deep ITM start)
    assert marks[-1].pnl_since_entry is not None
    assert marks[0].pnl_since_entry is not None


def test_insufficient_when_missing_spot():
    entry = date(2026, 1, 2)
    marks = simulate_trajectory(
        "long_straddle",
        entry_date=entry,
        as_of_dates=[entry],
        spots={},
        atm_ivs={},
    )
    assert marks[0].data_quality == "insufficient_chain"
    assert marks[0].pnl_since_entry is None


def test_locf_fill_iv_carries_within_gap():
    from bifrost_research.engines.canonical_pnl.compute import locf_fill_iv

    d0 = date(2026, 1, 2)
    d1 = date(2026, 1, 5)  # weekend gap
    d2 = date(2026, 1, 20)  # beyond 14d
    filled = locf_fill_iv({d0: 0.22}, [d0, d1, d2], max_gap_days=14)
    assert filled[d0] == 0.22
    assert filled[d1] == 0.22
    assert d2 not in filled


def test_all_structures_build():
    entry = date(2026, 3, 1)
    for s in STRUCTURES:
        legs, sp, _ = build_entry_legs(s, spot=150.0, atm_iv=0.28, entry_date=entry)
        assert legs
        assert default_params(s).structure == s
        assert sp.params_hash()
