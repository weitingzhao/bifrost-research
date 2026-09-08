"""The two widest lenses now measure themselves.

sepa and momentum cover 3,475 symbols and had no decay tracking, so the layer
with the widest reach never knew its own accuracy (blueprint C-F3 / C-R4).
"""

from __future__ import annotations

from bifrost_research.engines.signal_hit import build
from bifrost_research.lenses.registry import LENSES, SCORE_BANDS, decay_lens_ids


def test_sepa_and_momentum_are_decay_lenses_now() -> None:
    ids = decay_lens_ids()
    assert "sepa" in ids and "momentum" in ids
    assert LENSES["sepa"].hit_rule == "follow"
    assert LENSES["momentum"].hit_rule == "follow"


def test_sepa_hot_needs_a_buyable_path() -> None:
    hot = SCORE_BANDS.hot
    assert build.classify_sepa(hot, "PIVOT") == "hot"
    assert build.classify_sepa(hot, "SETUP") == "hot"
    # A high score on a name that already moved is not a setup.
    assert build.classify_sepa(hot, "EXTENDED") is None
    assert build.classify_sepa(hot, "AVOID") is None


def test_sepa_cold_is_the_score_band_alone() -> None:
    assert build.classify_sepa(SCORE_BANDS.cold, "AVOID") == "cold"
    assert build.classify_sepa(SCORE_BANDS.cold, "SETUP") == "cold"


def test_momentum_hot_needs_an_a_grade() -> None:
    hot = SCORE_BANDS.hot
    assert build.classify_momentum(hot, "A") == "hot"
    assert build.classify_momentum(hot, "A+") == "hot"
    assert build.classify_momentum(hot, "B") is None


def test_the_middle_of_the_band_is_no_trigger() -> None:
    mid = (SCORE_BANDS.hot + SCORE_BANDS.cold) / 2
    assert build.classify_sepa(mid, "PIVOT") is None
    assert build.classify_momentum(mid, "A") is None


def test_missing_values_never_trigger() -> None:
    assert build.classify_sepa(None, "PIVOT") is None
    assert build.classify_momentum(None, "A") is None
