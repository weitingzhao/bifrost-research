"""Pure-compute tests for Order Flow engine (no DB)."""

from __future__ import annotations

from datetime import date

from bifrost_research.engines.flow import (
    OptionFlowRow,
    compute_order_sentiment,
    detect_multi_leg_scaffolding,
)


def test_call_biased_sentiment() -> None:
    rows = [
        OptionFlowRow(
            expiry=date(2025, 6, 20),
            strike=100.0,
            option_right="C",
            volume=1000,
            open_interest=5000,
            mid_price=2.0,
        ),
        OptionFlowRow(
            expiry=date(2025, 6, 20),
            strike=100.0,
            option_right="P",
            volume=200,
            open_interest=4000,
            mid_price=1.5,
        ),
    ]
    s = compute_order_sentiment(rows)
    assert s["sentiment_score"] > 0
    assert s["call_notional"] > s["put_notional"]
    assert s["data_source"] == "option_snapshot_aggregates"
    assert "tape" in s["notes"].lower() or "Polygon" in s["notes"]


def test_tape_data_source_override() -> None:
    rows = [
        OptionFlowRow(
            expiry=date(2025, 6, 20),
            strike=100.0,
            option_right="C",
            volume=100,
            open_interest=0,
            mid_price=1.0,
        ),
    ]
    s = compute_order_sentiment(
        rows,
        data_source="option_trades_tape",
        notes="from tape",
    )
    assert s["data_source"] == "option_trades_tape"
    assert s["notes"] == "from tape"


def test_put_biased_sentiment() -> None:
    rows = [
        OptionFlowRow(
            expiry=date(2025, 6, 20),
            strike=95.0,
            option_right="C",
            volume=100,
            open_interest=1000,
            mid_price=1.0,
        ),
        OptionFlowRow(
            expiry=date(2025, 6, 20),
            strike=95.0,
            option_right="P",
            volume=2000,
            open_interest=8000,
            mid_price=3.0,
        ),
    ]
    s = compute_order_sentiment(rows)
    assert s["sentiment_score"] < 0
    assert s["pcr_volume"] is not None and s["pcr_volume"] > 1


def test_multi_leg_scaffolding_straddle() -> None:
    rows = [
        OptionFlowRow(
            expiry=date(2025, 6, 20),
            strike=100.0,
            option_right="C",
            volume=200,
            open_interest=1000,
            mid_price=2.0,
        ),
        OptionFlowRow(
            expiry=date(2025, 6, 20),
            strike=100.0,
            option_right="P",
            volume=180,
            open_interest=900,
            mid_price=2.1,
        ),
    ]
    clusters = detect_multi_leg_scaffolding(rows, min_volume=50)
    assert len(clusters) == 1
    assert clusters[0]["strategy_guess"] == "straddle_candidate"
    assert clusters[0]["confidence"] < 0.5  # scaffolding


def test_empty_flow() -> None:
    s = compute_order_sentiment([])
    assert s["sentiment_score"] == 0.0
    assert s["call_notional"] == 0.0
    assert detect_multi_leg_scaffolding([]) == []
