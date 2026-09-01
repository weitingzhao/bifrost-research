"""GET /research/objective-runs/{id} — LS-3a."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_get_objective_run_not_found(client: TestClient) -> None:
    with patch("bifrost_research.api.harness.connect") as mock_connect:
        conn = MagicMock()
        mock_connect.return_value = conn
        with patch("bifrost_research.api.harness.obj_repo.get_run", return_value=None):
            resp = client.get("/research/objective-runs/run_missing")
    assert resp.status_code == 404


def test_get_objective_run_ok(client: TestClient) -> None:
    run = {
        "id": "run_abc",
        "objective_id": "obj-1",
        "status": "awaiting_approval",
        "trace_json": {"events": []},
    }
    obj = {"id": "obj-1", "title": "Daily Stock", "policy_json": {}}
    with patch("bifrost_research.api.harness.connect") as mock_connect:
        conn = MagicMock()
        mock_connect.return_value = conn
        with patch("bifrost_research.api.harness.obj_repo.get_run", return_value=run):
            with patch("bifrost_research.api.harness.obj_repo.get_objective", return_value=obj):
                resp = client.get("/research/objective-runs/run_abc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["id"] == "run_abc"
    assert body["data"]["objective_title"] == "Daily Stock"
