"""Signal decay API helpers + intersect parsing (no DB)."""

from fastapi import HTTPException
import pytest

from bifrost_research.api.signal_decay import (
    VALID_LENSES,
    _hit_rates,
    _parse_lens_pairs,
    _profit_factor,
    _side_stats,
)


def test_valid_lenses() -> None:
    assert "iv_rank" in VALID_LENSES
    assert "vrp" in VALID_LENSES
    assert "opex_pin" in VALID_LENSES


def test_side_stats_rates() -> None:
    rows = [
        {"trigger_side": "hot", "hit_5d": True, "hit_20d": False},
        {"trigger_side": "hot", "hit_5d": False, "hit_20d": True},
        {"trigger_side": "hot", "hit_5d": None, "hit_20d": None},
        {"trigger_side": "cold", "hit_5d": True, "hit_20d": True},
    ]
    hot = _side_stats(rows, "hot", lens="iv_rank")
    assert hot["n"] == 3
    assert hot["hit_5d"] == 1
    assert hot["evaluated_5d"] == 2
    assert hot["pending_5d"] == 1
    assert hot["hit_rate_5d"] == 0.5
    cold = _side_stats(rows, "cold", lens="iv_rank")
    assert cold["hit_rate_5d"] == 1.0
    assert cold["pending_5d"] == 0


def test_parse_lens_pairs() -> None:
    pairs = _parse_lens_pairs("iv_rank:hot,vrp:cold")
    assert pairs == [("iv_rank", "hot"), ("vrp", "cold")]


def test_parse_lens_pairs_rejects_short() -> None:
    with pytest.raises(HTTPException) as ei:
        _parse_lens_pairs("iv_rank:hot")
    assert ei.value.status_code == 400


def test_hit_rates() -> None:
    rows = [
        {"hit_5d": True, "hit_20d": True},
        {"hit_5d": False, "hit_20d": None},
        {"hit_5d": None, "hit_20d": False},
    ]
    r = _hit_rates(rows)
    assert r["n"] == 3
    assert r["evaluated_5d"] == 2
    assert r["hit_rate_5d"] == 0.5
    assert r["evaluated_20d"] == 2
    assert r["hit_rate_20d"] == 0.5


def test_profit_factor_mean_revert_pays_hot_down_and_cold_up() -> None:
    rows = [
        {"trigger_side": "hot", "fwd_return_5d": -0.03},  # faded as called: +0.03
        {"trigger_side": "hot", "fwd_return_5d": 0.01},  # against: -0.01
        {"trigger_side": "cold", "fwd_return_5d": 0.02},  # bounced as called: +0.02
        {"trigger_side": "cold", "fwd_return_5d": -0.01},  # against: -0.01
    ]
    assert _profit_factor(rows, lens="iv_rank", horizon=5) == 2.5
    hot_only = [r for r in rows if r["trigger_side"] == "hot"]
    assert _profit_factor(hot_only, lens="iv_rank", horizon=5) == 3.0


def test_profit_factor_follow_pays_hot_up() -> None:
    rows = [
        {"trigger_side": "hot", "fwd_return_20d": 0.04},
        {"trigger_side": "hot", "fwd_return_20d": -0.01},
    ]
    assert _profit_factor(rows, lens="momentum", horizon=20) == 4.0
    # The same tape read as mean-revert is the mirror image.
    assert _profit_factor(rows, lens="iv_rank", horizon=20) == 0.25


def test_profit_factor_is_null_for_a_magnitude_lens() -> None:
    rows = [
        {"trigger_side": "hot", "fwd_return_5d": 0.05},
        {"trigger_side": "cold", "fwd_return_5d": -0.01},
    ]
    assert _profit_factor(rows, lens="gex_regime", horizon=5) is None


def test_profit_factor_is_null_without_losers_or_settled_rows() -> None:
    winners = [{"trigger_side": "hot", "fwd_return_5d": -0.02}]
    assert _profit_factor(winners, lens="iv_rank", horizon=5) is None
    assert _profit_factor([], lens="iv_rank", horizon=5) is None
    all_losers = [{"trigger_side": "hot", "fwd_return_5d": 0.02}]
    assert _profit_factor(all_losers, lens="iv_rank", horizon=5) == 0.0


def test_profit_factor_ignores_pending_rows() -> None:
    rows = [
        {"trigger_side": "hot", "fwd_return_5d": -0.02, "fwd_return_20d": None},
        {"trigger_side": "hot", "fwd_return_5d": 0.01, "fwd_return_20d": None},
        {"trigger_side": "hot", "fwd_return_5d": None, "fwd_return_20d": None},
    ]
    assert _profit_factor(rows, lens="iv_rank", horizon=5) == 2.0
    assert _profit_factor(rows, lens="iv_rank", horizon=20) is None


def test_side_stats_carry_profit_factor_per_side() -> None:
    rows = [
        {"trigger_side": "hot", "fwd_return_5d": -0.03, "fwd_return_20d": -0.05},
        {"trigger_side": "hot", "fwd_return_5d": 0.01, "fwd_return_20d": 0.01},
        {"trigger_side": "cold", "fwd_return_5d": 0.02, "fwd_return_20d": None},
    ]
    hot = _side_stats(rows, "hot", lens="iv_rank")
    assert hot["profit_factor_5d"] == 3.0
    assert hot["profit_factor_20d"] == 5.0
    cold = _side_stats(rows, "cold", lens="iv_rank")
    assert cold["profit_factor_5d"] is None
    assert cold["profit_factor_20d"] is None
