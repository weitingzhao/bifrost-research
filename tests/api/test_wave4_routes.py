"""Wave 4 route registration + compute endpoints (no DB)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app


def test_wave4_routes_registered() -> None:
    client = TestClient(create_app())
    paths = set(client.app.openapi()["paths"])
    assert "/research/forecast/terrain/compute" in paths
    assert "/research/forecast/terrain" in paths
    assert "/research/forecast/sessions/compute" in paths
    assert "/research/forecast/sessions" in paths
    assert "/research/forecast/hourly" in paths
    assert "/research/event-radar/run" in paths
    assert "/research/event-radar/events" in paths
    assert "/research/backtest/settle" in paths
    assert "/research/backtest/aggregate" in paths
    assert "/research/backtest/settlement" in paths
    assert "/research/daily-brief/synth" in paths
    assert "/research/backtest/regime-stats" in paths


def test_terrain_compute_endpoint() -> None:
    client = TestClient(create_app())
    resp = client.post(
        "/research/forecast/terrain/compute",
        json={
            "symbol": "SPY",
            "trade_date": "2024-06-03",
            "spot": 500.0,
            "gex": {
                "zero_gamma": 499.0,
                "major_call_wall": 510.0,
                "major_put_wall": 490.0,
                "total_net_gex": 1e9,
            },
            "momentum": {"score": 60, "path": "PB", "crash": 70},
            "iv": {"iv_percentile_1y": 40.0},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["terrain"]["symbol"] == "SPY"
    assert body["terrain"]["regime"] in {"crash-risk", "range", "trending"}


def test_forecast_compute_endpoint() -> None:
    client = TestClient(create_app())
    resp = client.post(
        "/research/forecast/sessions/compute",
        json={
            "symbol": "QQQ",
            "trade_date": "2024-06-03",
            "spot": 450.0,
            "enrich": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["hourly"]) == 6
    assert body["llm_provider"] == "heuristic"


def test_event_radar_run_endpoint() -> None:
    client = TestClient(create_app())
    resp = client.post(
        "/research/event-radar/run",
        json={
            "payload": "- Fed announced hold; $SPY rally.\n- Sources say IPO planned.",
            "source": "api-test",
            "collected_at": "2024-06-12",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["raw_count"] >= 2
    assert body["self_check"]["passed"] is True


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
