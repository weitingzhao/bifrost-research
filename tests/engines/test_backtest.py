"""Wave 4.4 Settlement / backtest accuracy tests."""

from __future__ import annotations

from datetime import date

from bifrost_research.engines.backtest.settlement import (
    aggregate_accuracy,
    settle_forecast,
)


def test_settle_close_miss_and_path() -> None:
    hourly = [
        {
            "hour_et": 10,
            "path_call": "mean-revert",
            "level_low": 99.0,
            "level_high": 101.0,
            "level_target": 100.0,
        },
        {
            "hour_et": 15,
            "path_call": "mean-revert->close",
            "level_low": 99.0,
            "level_high": 101.0,
            "level_target": 100.2,
        },
    ]
    stl = settle_forecast(
        session_id="s1",
        symbol="SPY",
        trade_date=date(2024, 6, 3),
        expected_close=100.0,
        hourly=hourly,
        actual_close=100.5,
        hourly_actuals={10: 100.1, 15: 100.4},
    )
    assert stl.close_miss == 0.5
    assert abs(stl.close_miss_pct - 0.005) < 1e-9
    assert stl.path_total == 2
    assert stl.path_hit_count >= 1
    assert "D10" in stl.notes


def test_aggregate_accuracy() -> None:
    a = settle_forecast(
        session_id="a",
        symbol="QQQ",
        trade_date=date(2024, 6, 3),
        expected_close=400.0,
        hourly=[
            {
                "hour_et": 12,
                "path_call": "coil",
                "level_low": 398,
                "level_high": 402,
                "level_target": 400,
            }
        ],
        actual_close=400.2,
        hourly_actuals={12: 400.1},
    )
    b = settle_forecast(
        session_id="b",
        symbol="QQQ",
        trade_date=date(2024, 6, 4),
        expected_close=401.0,
        hourly=[
            {
                "hour_et": 12,
                "path_call": "higher-high",
                "level_low": 400,
                "level_high": 410,
                "level_target": 405,
            }
        ],
        actual_close=390.0,
        hourly_actuals={12: 392.0},
    )
    summary = aggregate_accuracy([a, b], symbol="QQQ")
    assert summary.sessions_settled == 2
    assert 0.0 <= summary.path_hit_rate <= 1.0
    assert summary.avg_close_miss_pct >= 0
    assert summary.period_start <= summary.period_end
