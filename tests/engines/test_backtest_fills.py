"""Tests for the RS-C2 realistic fill model."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from bifrost_research.engines.backtest.event_defs import EventDef
from bifrost_research.engines.backtest.event_query import run_event_query
from bifrost_research.engines.backtest.fills import (
    DEFAULT_FILL_CONFIG,
    FillConfig,
    apply_commission,
    compute_fill_price,
)

from tests.engines.test_backtest_event_query import (
    _build_state_with_earnings_and_options,
    _FakeConn,
)


# ---------------------------------------------------------------------------
# Unit — compute_fill_price
# ---------------------------------------------------------------------------


def test_default_slippage_matches_spec() -> None:
    """Spec example: (bid=1.0, ask=1.2, close=1.1) → buy 1.14 / sell 1.06."""
    cfg = FillConfig()  # default slippage_pct_of_spread=0.2
    buy = compute_fill_price("buy", bid=1.0, ask=1.2, close=1.1, config=cfg)
    sell = compute_fill_price("sell", bid=1.0, ask=1.2, close=1.1, config=cfg)
    assert buy == pytest.approx(1.14, abs=1e-9)
    assert sell == pytest.approx(1.06, abs=1e-9)


def test_zero_bid_ask_falls_back_to_close_with_no_slippage() -> None:
    """When bid/ask missing → uses close, zero slippage (backward compatible)."""
    buy = compute_fill_price("buy", bid=0.0, ask=0.0, close=1.1)
    sell = compute_fill_price("sell", bid=0.0, ask=0.0, close=1.1)
    assert buy == 1.1
    assert sell == 1.1


def test_none_bid_ask_falls_back_to_close() -> None:
    buy = compute_fill_price("buy", bid=None, ask=None, close=2.5)  # type: ignore[arg-type]
    assert buy == 2.5


def test_negative_close_clamped_to_zero() -> None:
    price = compute_fill_price("sell", bid=0.0, ask=0.0, close=-1.0)
    assert price == 0.0


def test_config_default_slippage_matches_spec_0_2() -> None:
    assert DEFAULT_FILL_CONFIG.slippage_pct_of_spread == 0.2
    assert DEFAULT_FILL_CONFIG.commission_per_contract == 0.65
    assert DEFAULT_FILL_CONFIG.multiplier == 100
    assert DEFAULT_FILL_CONFIG.exercise_style == "american_no_early"


def test_invalid_side_raises() -> None:
    with pytest.raises(ValueError):
        compute_fill_price("long", bid=1.0, ask=1.2, close=1.1)  # type: ignore[arg-type]


def test_apply_commission_round_trip() -> None:
    cfg = FillConfig(commission_per_contract=0.65)
    # 2 contracts × 2 sides × 0.65 = 2.60
    assert apply_commission(2, cfg) == pytest.approx(2.60)
    # sides=1 (single side)
    assert apply_commission(2, cfg, sides=1) == pytest.approx(1.30)


def test_slippage_pct_zero_yields_mid() -> None:
    cfg = FillConfig(slippage_pct_of_spread=0.0)
    p = compute_fill_price("buy", bid=1.0, ask=1.2, close=1.1, config=cfg)
    assert p == pytest.approx(1.1)


# ---------------------------------------------------------------------------
# Integration — event_query threads FillConfig through option leg pricing
# ---------------------------------------------------------------------------


def test_event_query_applies_commission_when_config_passed() -> None:
    """A long_atm_call with commission should be lower than without."""
    today = date(2026, 6, 1)
    events = [today - timedelta(days=45)]
    state = _build_state_with_earnings_and_options(events, symbol="NVDA")

    conn = _FakeConn(state)
    baseline = run_event_query(
        EventDef(kind="earnings", params={"symbols": ["NVDA"]}),
        template_name="long_atm_call",
        lookback_years=1,
        conn=conn,
        today=today,
    )
    baseline_pnl = baseline["runs"][0]["pnl"]

    conn2 = _FakeConn(state)
    with_fills = run_event_query(
        EventDef(kind="earnings", params={"symbols": ["NVDA"]}),
        template_name="long_atm_call",
        lookback_years=1,
        conn=conn2,
        today=today,
        fill_config=FillConfig(commission_per_contract=0.65),
    )
    with_pnl = with_fills["runs"][0]["pnl"]

    # 1 contract × 2 sides × $0.65 = $1.30 subtracted from gross P&L
    assert with_pnl == pytest.approx(baseline_pnl - 1.30, abs=1e-6)
    assert with_fills["runs"][0]["legs"][0]["fill_details"]["commission"] == pytest.approx(1.30)


def test_event_query_backward_compat_no_fill_config() -> None:
    """Without FillConfig, RS-C1 default behavior is preserved."""
    today = date(2026, 6, 1)
    events = [today - timedelta(days=45)]
    state = _build_state_with_earnings_and_options(events, symbol="NVDA")

    conn = _FakeConn(state)
    result = run_event_query(
        EventDef(kind="earnings", params={"symbols": ["NVDA"]}),
        template_name="long_atm_call",
        lookback_years=1,
        conn=conn,
        today=today,
    )
    assert result["runs"][0]["legs"][0]["fill_details"]["commission"] == 0.0
