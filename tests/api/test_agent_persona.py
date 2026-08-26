"""Agent persona API tests — mocked repository (Wave RS-PS1)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app


@pytest.fixture(autouse=True)
def _patch_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bifrost_research.api.health.run_startup_schema_guard",
        lambda: None,
    )
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_list_personas_seeds_and_returns(client: TestClient) -> None:
    sample = {
        "owner_id": "owner",
        "agent_name": "portfolio",
        "persona_md": "# Portfolio",
        "preferences_json": {},
        "guardrail_locked": False,
        "seeded": True,
        "updated_at": "2026-08-26T00:00:00+00:00",
    }
    with (
        patch("bifrost_research.api.agent_persona.connect", return_value=MagicMock()),
        patch("bifrost_research.repositories.agent_persona.list_for_owner", return_value=[sample]),
    ):
        res = client.get("/research/agent_persona")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert len(body["agents"]) == 1
    assert body["agents"][0]["agent_name"] == "portfolio"


def test_put_persona(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    updated = {
        "owner_id": "owner",
        "agent_name": "discovery",
        "persona_md": "custom",
        "preferences_json": {"favor_signals": ["breakout"]},
        "guardrail_locked": False,
        "seeded": False,
        "updated_at": "2026-08-26T00:00:00+00:00",
    }
    mock_conn = MagicMock()
    monkeypatch.setattr("bifrost_research.api.agent_persona.connect", lambda: mock_conn)
    monkeypatch.setattr(
        "bifrost_research.repositories.agent_persona.get",
        lambda conn, owner_id, agent_name: updated,
    )
    monkeypatch.setattr(
        "bifrost_research.repositories.agent_persona.upsert",
        lambda conn, owner_id, agent_name, **kw: updated,
    )
    res = client.put(
        "/research/agent_persona/discovery",
        json={"persona_md": "custom", "preferences_json": {"favor_signals": ["breakout"]}},
    )
    assert res.status_code == 200
    assert res.json()["persona"]["persona_md"] == "custom"
