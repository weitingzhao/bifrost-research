"""Scan API helper tests — Analyze Wave D/H."""

from __future__ import annotations

import pytest

from bifrost_research.api.scan import (
    COMPOSITE_PRESETS,
    FLAG_KEYS,
    SORT_COLUMNS,
    merge_flag_filters,
    parse_flag_filter,
    recompute_composite,
    resolve_preset,
    resolve_sort_column,
)


def test_sort_columns_whitelist() -> None:
    assert resolve_sort_column("composite_score") == "composite_score"
    assert resolve_sort_column(" iv_rank_1y ") == "iv_rank_1y"
    with pytest.raises(ValueError, match="unsupported sort_by"):
        resolve_sort_column("'; DROP TABLE scan; --")


def test_parse_flag_filter_and_pairs() -> None:
    pairs = parse_flag_filter(" iv_rank:hot , vrp:cold ")
    assert pairs == [("iv_rank", "hot"), ("vrp", "cold")]
    assert parse_flag_filter("") == []
    assert parse_flag_filter(None) == []


def test_parse_flag_filter_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="unknown flag key"):
        parse_flag_filter("bogus:hot")


def test_parse_flag_filter_rejects_bad_segment() -> None:
    with pytest.raises(ValueError, match="invalid flag filter segment"):
        parse_flag_filter("iv_rank")


def test_flag_keys_cover_lens_flags() -> None:
    assert "iv_rank" in FLAG_KEYS
    assert "terrain" in FLAG_KEYS
    assert "composite_score" in SORT_COLUMNS


def test_merge_flag_filters_and_semantics() -> None:
    pairs = merge_flag_filters(
        "iv_rank:hot",
        vrp="cold",
        atm_slope="all",
        pin="neutral",
    )
    by_key = dict(pairs)
    assert by_key["iv_rank"] == "hot"
    assert by_key["vrp"] == "cold"
    assert by_key["pin"] == "neutral"
    assert "atm_slope" not in by_key


def test_merge_flag_filters_query_overrides_flag_filter() -> None:
    pairs = merge_flag_filters("iv_rank:hot", iv_rank="cold")
    assert dict(pairs)["iv_rank"] == "cold"


def test_resolve_preset_weights() -> None:
    name, weights = resolve_preset("momentum")
    assert name == "momentum"
    assert weights == COMPOSITE_PRESETS["momentum"]
    assert sum(weights.values()) == 100
    name2, w2 = resolve_preset("mean_revert")
    assert name2 == "mean_revert"
    assert w2["iv_rank"] == 35
    with pytest.raises(ValueError, match="unsupported preset"):
        resolve_preset("bogus")


def test_recompute_composite_presets_change_rank() -> None:
    row = {
        "iv_rank_1y": 90.0,
        "vrp_pct_252d": 10.0,
        "atm_slope_30d": 0.2,
        "pin_pct_distance": 0.0,
        "pin_score": 80.0,
    }
    neutral = recompute_composite(row, COMPOSITE_PRESETS["neutral"])
    momentum = recompute_composite(row, COMPOSITE_PRESETS["momentum"])
    mean_revert = recompute_composite(row, COMPOSITE_PRESETS["mean_revert"])
    assert neutral is not None and momentum is not None and mean_revert is not None
    assert momentum != mean_revert
    assert abs(momentum - neutral) > 0.01 or abs(mean_revert - neutral) > 0.01
