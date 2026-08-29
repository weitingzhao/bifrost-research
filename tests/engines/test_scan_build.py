"""Unit tests for Wave D scanner build helpers."""

from __future__ import annotations

from datetime import date

from bifrost_research.engines.scan.build import (
    build_lens_flags,
    build_scan_row,
    compute_composite,
    flag_for_score,
)


def test_flag_for_score_hot_cold_neutral() -> None:
    assert flag_for_score(85) == "hot"
    assert flag_for_score(80) == "hot"
    assert flag_for_score(15) == "cold"
    assert flag_for_score(20) == "cold"
    assert flag_for_score(50) == "neutral"
    assert flag_for_score(40) == "neutral"
    assert flag_for_score(60) == "neutral"
    assert flag_for_score(70) is None
    assert flag_for_score(None) is None


def test_compute_composite_weighted_average() -> None:
    parts = {
        "iv_rank_1y": 80.0,
        "vrp_pct_252d": 60.0,
        "atm_slope_30d": 0.0,
        "pin_pct_distance": 0.0,
        "pin_score": 70.0,
    }
    score = compute_composite(parts)
    assert score is not None
    assert 60.0 <= score <= 75.0


def test_compute_composite_uses_default_terrain_when_missing() -> None:
    parts = {
        "iv_rank_1y": 100.0,
        "vrp_pct_252d": 100.0,
    }
    score = compute_composite(parts)
    assert score is not None
    assert score > 80.0


def test_build_lens_flags_sparse() -> None:
    flags = build_lens_flags(
        iv_rank_1y=90.0,
        vrp_pct_252d=10.0,
        atm_slope_30d=0.0,
        pin_pct_distance=0.0,
        pin_score=75.0,
    )
    assert flags["iv_rank"] == "hot"
    assert flags["vrp"] == "cold"
    assert flags["atm_slope"] == "neutral"
    assert flags["pin"] == "neutral"
    assert "terrain" not in flags


def test_build_scan_row_shape() -> None:
    row = build_scan_row(
        trade_date=date(2026, 8, 22),
        symbol="nvda",
        iv_rank_1y=88.0,
        vrp_pct_252d=12.0,
        pin_score=62.0,
    )
    assert row["symbol"] == "NVDA"
    assert row["trade_date"] == date(2026, 8, 22)
    assert row["composite_score"] is not None
    assert isinstance(row["lens_flags"], dict)
    assert row["lens_flags"]["iv_rank"] == "hot"
