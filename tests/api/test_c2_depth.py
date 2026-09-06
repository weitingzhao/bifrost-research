"""research-loop-automation C2 — the pure halves of the per-lens depth work."""

from __future__ import annotations

from bifrost_research.api.exhibit import fwd20_by_band
from bifrost_research.api.exhibit_lenses import pin_history, term_structure_label
from bifrost_research.api.signal_decay import by_symbol_records
from bifrost_research.api.wave4 import calibration_rows
from bifrost_research.lenses.registry import classify


def test_term_structure_label_reads_near_minus_far() -> None:
    assert term_structure_label(0.025) == "backwardation"
    assert term_structure_label(-0.04) == "contango"
    assert term_structure_label(0.0) == "flat"
    assert term_structure_label(None) is None
    # The registry judges the same number the same way.
    assert classify("term_slope", 0.025) == "hot"
    assert classify("term_slope", -0.04) == "cold"
    assert classify("term_slope", 0.0) == "neutral"


def test_skew_is_a_score_on_its_own_percentile() -> None:
    assert classify("skew", 85.0) == "hot"
    assert classify("skew", 65.0) == "lean_hot"
    assert classify("skew", 50.0) == "neutral"
    assert classify("skew", 15.0) == "cold"


def test_pin_history_counts_settled_cycles_within_half_a_percent() -> None:
    rows = [{"pct_distance": 0.004}, {"pct_distance": -0.0049}, {"pct_distance": 0.02}, {"pct_distance": "n/a"}]
    h = pin_history(rows)
    assert (h["cycles"], h["pinned"]) == (3, 2)
    assert abs(h["pin_rate"] - 2 / 3) < 1e-9
    assert h["median_abs_distance"] == 0.0049
    assert pin_history([]) == {"cycles": 0, "pin_rate": None, "pinned_within": 0.005}


def test_fwd20_by_band_shapes_the_aggregate_and_keeps_empty_sides_honest() -> None:
    out = fwd20_by_band((12, 0.0142, 0.583, 0, None, None))
    assert out["hot"] == {"n": 12, "median_fwd": 0.0142, "share_positive": 0.583}
    assert out["cold"] == {"n": 0, "median_fwd": None, "share_positive": None}
    assert out["horizon"] == 20


def test_by_symbol_records_group_sides_under_each_symbol() -> None:
    rows = [
        ("nvda", "hot", 6, 6, 4, 4, 1),
        ("NVDA", "cold", 3, 3, 1, 0, 0),
        ("SPY", "cold", 2, 0, 0, 0, 0),
    ]
    out = by_symbol_records(rows)
    assert out["NVDA"]["hot"]["hit_rate_5d"] == 4 / 6
    assert out["NVDA"]["hot"]["hit_rate_20d"] == 0.25
    assert out["NVDA"]["cold"]["hit_rate_20d"] is None
    assert out["SPY"]["cold"] == {"n": 2, "evaluated_5d": 0, "hit_rate_5d": None, "evaluated_20d": 0, "hit_rate_20d": None}


def test_calibration_rows_put_confidence_next_to_the_record() -> None:
    rows = calibration_rows([("range", 13, 8, 0.58, 0.012), ("trending", 4, 3, 0.71, None), (None, 1, 0, None, None)])
    assert rows[0]["regime"] == "range" and rows[0]["n"] == 13
    assert abs(rows[0]["hit_rate"] - 8 / 13) < 1e-9
    assert abs(rows[0]["calibration_gap"] - (8 / 13 - 0.58)) < 1e-9
    assert rows[1]["avg_close_miss_pct"] is None
    assert rows[2] == {"regime": "unknown", "n": 1, "hits": 0, "hit_rate": 0.0, "avg_top_prob": None, "calibration_gap": None, "avg_close_miss_pct": None}
