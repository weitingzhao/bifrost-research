"""Wave 4 route registration + compute endpoints (no DB)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app


def test_wave4_routes_registered() -> None:
    client = TestClient(create_app())
    paths = set(client.app.openapi()["paths"])
    assert "/research/forecast/terrain" in paths
    assert "/research/forecast/sessions" in paths
    assert "/research/event-radar/events" in paths
    assert "/research/backtest/settle" in paths
    assert "/research/backtest/settlement" in paths
    assert "/research/daily-brief/synth" in paths
    for gone in (
        "/research/forecast/terrain/compute",
        "/research/forecast/sessions/compute",
        "/research/forecast/hourly",
        "/research/event-radar/run",
        "/research/events/ingest",
        "/research/backtest/aggregate",
        "/research/forecast/settlement",
        "/research/forecast/settle",
        "/research/forecast/backtest",
        "/research/backtest/regime-stats",
    ):
        assert gone not in paths


def test_backtest_settle_endpoint() -> None:
    client = TestClient(create_app())
    resp = client.post(
        "/research/backtest/settle",
        json={
            "session_id": "s-test",
            "symbol": "SPY",
            "trade_date": "2024-06-03",
            "expected_close": 100.0,
            "actual_close": 100.5,
            "hourly": [
                {
                    "hour_et": 15,
                    "path_call": "mean-revert->close",
                    "level_low": 99,
                    "level_high": 101,
                    "level_target": 100,
                }
            ],
            "hourly_actuals": {"15": 100.4},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["close_miss"] == 0.5
    assert body["path_total"] == 1
