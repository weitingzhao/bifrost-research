"""Signal decay API helpers + intersect parsing (no DB)."""

from fastapi import HTTPException
import pytest

from bifrost_research.api.signal_decay import (
    VALID_LENSES,
    _hit_rates,
    _parse_lens_pairs,
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
    hot = _side_stats(rows, "hot")
    assert hot["n"] == 3
    assert hot["hit_5d"] == 1
    assert hot["evaluated_5d"] == 2
    assert hot["pending_5d"] == 1
    assert hot["hit_rate_5d"] == 0.5
    cold = _side_stats(rows, "cold")
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
