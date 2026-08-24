"""Health endpoint smoke test (no DB required)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from bifrost_research import __version__
from bifrost_research.api.app import create_app


def test_health() -> None:
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["domain"] == "research"
    assert body["version"] == __version__
