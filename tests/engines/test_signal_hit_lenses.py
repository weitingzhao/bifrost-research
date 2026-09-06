"""A3 — the four new decay lenses and the hit rule each one carries."""

from __future__ import annotations

import pytest

from bifrost_research.engines.signal_hit import entry
from bifrost_research.engines.signal_hit.build import (
    classify_gex_regime,
    classify_order_sentiment,
    classify_skew,
    classify_terrain_regime,
    hit_for,
    magnitude_hit,
)
from bifrost_research.lenses.registry import LENSES, decay_lens_ids


def test_decay_lenses_are_the_registry_and_the_builder_knows_each() -> None:
    assert set(decay_lens_ids()) == {
        "iv_rank", "vrp", "opex_pin", "skew", "gex_regime", "terrain_regime", "order_sentiment",
    }
    assert tuple(entry.ALL_LENSES) == decay_lens_ids()
    assert entry._parse_lenses("skew,gex_regime") == ["skew", "gex_regime"]
    with pytest.raises(ValueError, match="unknown lens"):
        entry._parse_lenses("moon_phase")


def test_skew_is_contrarian_on_the_sign_of_an_extreme() -> None:
    # C2: extreme means the top band of the symbol's own 252-day percentile.
    assert classify_skew(-0.30, 92.0) == "hot"  # call-skew extreme expects the price down
    assert classify_skew(0.30, 85.0) == "cold"  # put-skew extreme expects it up
    assert classify_skew(0.30, 55.0) is None  # a big slope that is normal for this name
    assert classify_skew(-0.02, 95.0) == "hot"  # a small slope that is extreme for this name
    assert classify_skew(-0.30, 95.0, history_days=20) is None  # too little history
    assert classify_skew(-0.30, None) is None
    assert classify_skew(None, 95.0) is None
    assert hit_for("skew", side="hot", fwd_return=-0.02, horizon=5) is True
    assert hit_for("skew", side="cold", fwd_return=-0.02, horizon=5) is False


def test_gex_regime_is_a_magnitude_rule() -> None:
    assert classify_gex_regime(-5.0e8) == "hot"
    assert classify_gex_regime(2.0e8) == "cold"
    assert classify_gex_regime(None) is None
    assert LENSES["gex_regime"].move_threshold == (0.02, 0.04)
    assert hit_for("gex_regime", side="hot", fwd_return=0.025, horizon=5) is True
    assert hit_for("gex_regime", side="hot", fwd_return=-0.025, horizon=20) is False
    assert hit_for("gex_regime", side="cold", fwd_return=0.01, horizon=5) is True
    assert hit_for("gex_regime", side="cold", fwd_return=0.05, horizon=20) is False
    assert magnitude_hit(side="hot", fwd_return=None, threshold=0.02) is None


def test_terrain_only_fires_on_crash_risk_and_expects_down() -> None:
    assert classify_terrain_regime("crash-risk") == "hot"
    assert classify_terrain_regime("Crash-Risk") == "hot"
    assert classify_terrain_regime("range") is None
    assert classify_terrain_regime("trending") is None
    assert classify_terrain_regime(None) is None
    assert hit_for("terrain_regime", side="hot", fwd_return=-0.03, horizon=5) is True
    assert hit_for("terrain_regime", side="hot", fwd_return=0.03, horizon=5) is False


def test_order_sentiment_triggers_only_from_the_tape_and_is_followed() -> None:
    assert classify_order_sentiment(45.0, "option_snapshot_aggregates") is None
    assert classify_order_sentiment(45.0, None) is None
    assert classify_order_sentiment(45.0, "option_trades_tape") == "hot"
    assert classify_order_sentiment(-40.0, "option_trades_tape") == "cold"
    assert classify_order_sentiment(0.0, "option_trades_tape") is None
    assert hit_for("order_sentiment", side="hot", fwd_return=0.01, horizon=5) is True
    assert hit_for("order_sentiment", side="cold", fwd_return=0.01, horizon=5) is False
    assert hit_for("order_sentiment", side="hot", fwd_return=None, horizon=5) is None


def test_wave_i_lenses_keep_the_mean_revert_rule() -> None:
    assert hit_for("iv_rank", side="hot", fwd_return=-0.01, horizon=5) is True
    assert hit_for("vrp", side="cold", fwd_return=0.01, horizon=20) is True
    assert hit_for("opex_pin", side="hot", fwd_return=0.01, horizon=5) is False
    assert hit_for("momentum", side="hot", fwd_return=0.01, horizon=5) is None
