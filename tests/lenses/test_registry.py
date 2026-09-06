"""Lens registry — the bands the engines apply, and every consumer reading them."""

from __future__ import annotations

from typing import get_args

import pytest

from bifrost_research.api.similar_regime import Lens as SimilarLens
from bifrost_research.engines.alert_scan import entry as alert_entry
from bifrost_research.engines.scan.build import flag_for_score
from bifrost_research.engines.signal_hit.build import (
    classify_iv_rank,
    classify_opex_pin,
    classify_vrp,
)
from bifrost_research.lenses.registry import (
    LENSES,
    band_for_score,
    classify,
    decay_lens_ids,
    public_registry,
    scan_flag,
    similar_lens_ids,
    trigger_side,
)


def test_score_bands_cover_the_scale_without_gaps() -> None:
    assert band_for_score(80) == "hot"
    assert band_for_score(79.9) == "lean_hot"
    assert band_for_score(60.1) == "lean_hot"
    assert band_for_score(60) == "neutral"
    assert band_for_score(40) == "neutral"
    assert band_for_score(39.9) == "lean_cold"
    assert band_for_score(20.1) == "lean_cold"
    assert band_for_score(20) == "cold"
    assert band_for_score(None) is None


def test_fractions_read_as_percent_only_when_asked() -> None:
    assert classify("iv_rank", 0.85, fractions_as_pct=True) == "hot"
    assert classify("iv_rank", 0.85) == "cold"
    assert classify("vrp", 1.0, fractions_as_pct=True) == "hot"


def test_severity_and_distance_kinds() -> None:
    # C2: skew is judged on its own 252-day percentile, so it reads as a score.
    assert classify("skew", 85.0) == "hot"
    assert classify("skew", 65.0) == "lean_hot"
    assert classify("skew", 50.0) == "neutral"
    assert classify("skew", 15.0) == "cold"
    assert classify("opex_pin", 0.010) == "hot"
    assert classify("opex_pin", -0.0099) == "hot"
    assert classify("opex_pin", 0.011) == "neutral"


def test_signed_and_categorical_kinds() -> None:
    assert classify("order_sentiment", 30) == "hot"
    assert classify("order_sentiment", -30) == "cold"
    assert classify("order_sentiment", 0) == "neutral"
    assert classify("terrain_regime", 1.0) is None
    # C2: term_slope judged on near − far vol — backwardation hot, steep contango cold.
    assert classify("term_slope", 0.4) == "hot"
    assert classify("term_slope", -0.05) == "cold"
    assert classify("term_slope", 0.0) == "neutral"


def test_unknown_lens_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown lens"):
        classify("moon_phase", 1.0)


def test_signal_hit_classifiers_are_the_registry() -> None:
    for v in [None, 0.0, 0.2, 0.5, 0.8, 1.0, 19.9, 20.0, 50.0, 79.9, 80.0, 100.0]:
        assert classify_iv_rank(v) == trigger_side("iv_rank", v, fractions_as_pct=True)
        assert classify_vrp(v) == trigger_side("vrp", v, fractions_as_pct=True)
    for d in [None, 0.0, 0.005, 0.01, 0.0101, -0.02]:
        assert classify_opex_pin(d) == trigger_side("opex_pin", d)
    assert classify_iv_rank(80) == "hot"
    assert classify_iv_rank(20) == "cold"
    assert classify_iv_rank(50) is None
    assert classify_opex_pin(0.02) is None


def test_scan_flags_are_the_registry_and_keep_their_sparse_shape() -> None:
    for v in range(0, 101):
        assert flag_for_score(v) == scan_flag(band_for_score(v))
    assert flag_for_score(80) == "hot"
    assert flag_for_score(20) == "cold"
    assert flag_for_score(40) == "neutral"
    assert flag_for_score(60) == "neutral"
    assert flag_for_score(70) is None
    assert flag_for_score(30) is None
    assert flag_for_score(None) is None


def test_alert_and_similar_regime_read_the_registry() -> None:
    assert alert_entry.LENSES == decay_lens_ids()
    assert {"iv_rank", "vrp", "opex_pin"} <= set(decay_lens_ids())
    for lens in decay_lens_ids():
        assert LENSES[lens].hit_rule != "none"
    assert set(get_args(SimilarLens)) == set(similar_lens_ids())


def test_public_registry_is_complete_and_routable() -> None:
    rows = public_registry()
    assert len(rows) >= 10
    ids = {r["id"] for r in rows}
    assert {"iv_rank", "vrp", "skew", "opex_pin", "gex_regime", "terrain_regime", "order_sentiment"} <= ids
    for r in rows:
        assert r["page_route"].startswith("/research/")
        assert r["label"] and r["source_table"] and r["hot_means"]
        assert set(r["bands"]) == {"hot", "lean_hot", "lean_cold", "cold"}
    sentiment = next(r for r in rows if r["id"] == "order_sentiment")
    assert sentiment["data_dependency"] == "option_trades_tape"
    assert LENSES["iv_rank"].bands.hot == 80.0
