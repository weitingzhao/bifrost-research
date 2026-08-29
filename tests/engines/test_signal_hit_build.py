"""Unit tests for signal_hit pure helpers."""

from bifrost_research.engines.signal_hit.build import (
    classify_iv_rank,
    classify_opex_pin,
    classify_vrp,
    side_aware_hit,
)


def test_classify_iv_rank_thresholds() -> None:
    assert classify_iv_rank(85) == "hot"
    assert classify_iv_rank(10) == "cold"
    assert classify_iv_rank(50) is None
    assert classify_iv_rank(0.9) == "hot"  # 0-1 scale
    assert classify_iv_rank(0.1) == "cold"


def test_classify_vrp() -> None:
    assert classify_vrp(90) == "hot"
    assert classify_vrp(5) == "cold"
    assert classify_vrp(None) is None


def test_classify_opex_pin() -> None:
    assert classify_opex_pin(0.001) == "hot"
    assert classify_opex_pin(-0.004) == "hot"
    assert classify_opex_pin(0.009) == "hot"  # Wave J threshold 0.010
    assert classify_opex_pin(0.02) is None


def test_side_aware_hit() -> None:
    assert side_aware_hit(side="hot", fwd_return=-0.01) is True
    assert side_aware_hit(side="hot", fwd_return=0.02) is False
    assert side_aware_hit(side="cold", fwd_return=0.03) is True
    assert side_aware_hit(side="cold", fwd_return=-0.01) is False
    assert side_aware_hit(side="hot", fwd_return=None) is None
