"""W2 — every evaluation says which class it is."""

from __future__ import annotations

from bifrost_research.engines.backtest.catalog import EVALUATIONS, evaluation


def test_only_position_replays_are_called_backtests() -> None:
    classes = {k: v["class"] for k, v in EVALUATIONS.items()}
    assert {k for k, c in classes.items() if c == "backtest"} == {"option_simulator", "event_backtest", "suggestion_settlement"}
    assert classes["canonical_pnl"] == "model_reference"
    for key in ("lens_hit_rate", "candidate_outcome", "forecast_settlement", "indicator_signal", "pine_signal"):
        assert classes[key] == "signal_evaluation"


def test_the_stamp_carries_class_title_and_price_basis() -> None:
    stamp = evaluation("canonical_pnl")
    assert stamp["key"] == "canonical_pnl" and stamp["class"] == "model_reference"
    assert "Black–Scholes" in stamp["price_basis"]
