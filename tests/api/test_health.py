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


def test_health_says_which_path_the_persona_chain_takes(monkeypatch) -> None:
    client = TestClient(create_app())
    monkeypatch.delenv("BIFROST_PERSONA_EVAL_SKIP_AGENT", raising=False)
    monkeypatch.delenv("BIFROST_PERSONA_EVAL_AGENTS", raising=False)
    assert client.get("/health").json()["persona_eval_agents"] is False
    monkeypatch.setenv("BIFROST_PERSONA_EVAL_AGENTS", "1")
    assert client.get("/health").json()["persona_eval_agents"] is True
    # The skip switch wins, as it does where the chain actually runs.
    monkeypatch.setenv("BIFROST_PERSONA_EVAL_SKIP_AGENT", "1")
    assert client.get("/health").json()["persona_eval_agents"] is False
