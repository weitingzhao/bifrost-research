"""GET /research/lenses — the registry as the frontend reads it (no database)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app


@pytest.fixture(autouse=True)
def _patch_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bifrost_research.api.health.run_startup_schema_guard", lambda: None)
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


def test_lenses_endpoint_lists_bands_and_routes() -> None:
    client = TestClient(create_app())
    res = client.get("/research/lenses")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["count"] == len(data["lenses"]) >= 10
    assert data["score_bands"] == {"hot": 80.0, "lean_hot": 60.0, "lean_cold": 40.0, "cold": 20.0}
    iv = next(r for r in data["lenses"] if r["id"] == "iv_rank")
    assert iv["bands"]["hot"] == 80.0
    assert iv["page_route"] == "/research/vol-regime?view=iv-rank"  # C1 hub view
    assert iv["decay_lens"] == "iv_rank" and iv["scan_flag"] == "iv_rank"
