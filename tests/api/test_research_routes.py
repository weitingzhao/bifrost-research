"""Wave 3 research engine route registration (no DB)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app


def test_research_routes_registered() -> None:
    client = TestClient(create_app())
    paths = set(client.app.openapi()["paths"])
    assert "/research/momentum/radar" in paths
    assert "/research/gex/levels" in paths
    assert "/research/gex/distribution" in paths
    assert "/research/volatility/smile" in paths
    assert "/research/volatility/surface" in paths
    assert "/research/flow/sentiment" in paths
    assert "/research/flow/multi-leg" in paths
    # Wave 4
    assert "/research/forecast/terrain/compute" in paths
    assert "/research/event-radar/run" in paths
    assert "/research/backtest/settle" in paths
