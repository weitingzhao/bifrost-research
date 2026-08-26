"""Bridge API tests — Wave RS-EX2 (mocked LLM + session)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.copilot.bridge_rate_limit import reset_bridge_rate_limit_for_tests
from bifrost_research.copilot.providers import ProviderTurn
from bifrost_research.copilot.rate_limit import reset_usage_for_tests


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_usage_for_tests()
    reset_bridge_rate_limit_for_tests()
    monkeypatch.setattr(
        "bifrost_research.api.health.run_startup_schema_guard",
        lambda: None,
    )
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None
    yield
    reset_usage_for_tests()
    reset_bridge_rate_limit_for_tests()


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_bridge_presets(client: TestClient) -> None:
    res = client.get("/research/copilot/bridge/presets")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    data = body["data"]
    assert len(data["focuses"]) == 4
    assert data["default_model"] == "deepseek-chat"


def test_bridge_session_mocked(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = "11111111-1111-1111-1111-111111111111"

    class _FakeConn:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "bifrost_research.copilot.bridge_runtime.connect",
        lambda: _FakeConn(),
    )
    monkeypatch.setattr(
        "bifrost_research.copilot.bridge_runtime.session_repo.get_session",
        lambda conn, sid, owner_id=None: {
            "id": sid,
            "title": "Test",
            "messages": [
                {"kind": "text", "role": "user", "content": "How is SPY risk?"},
                {
                    "kind": "tool_result",
                    "tool_name": "trade.portfolio.risk_summary",
                    "tool_call_id": "tc1",
                    "ok": True,
                    "data": {"symbol": "SPY", "daily_pnl": 10},
                },
                {"kind": "text", "role": "assistant", "content": "SPY daily PnL is +$10."},
            ],
        },
    )

    def _fake_insert(conn, **kwargs: Any) -> dict[str, Any]:
        return {
            "id": "22222222-2222-2222-2222-222222222222",
            "created_at": "2026-08-26T12:00:00+00:00",
            **{k: v for k, v in kwargs.items() if k != "conn"},
        }

    monkeypatch.setattr(
        "bifrost_research.copilot.bridge_runtime.bridge_repo.insert_event",
        _fake_insert,
    )
    monkeypatch.setattr(
        "bifrost_research.copilot.bridge_runtime.session_repo.append_message",
        lambda *a, **k: None,
    )

    class _StubProvider:
        def complete(self, *, messages: list[dict[str, Any]], model: str) -> ProviderTurn:
            return ProviderTurn(
                text="# Context for external AI\n\nSPY risk looks fine.",
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.0001,
            )

    monkeypatch.setattr(
        "bifrost_research.copilot.bridge_runtime._resolve_chat_provider",
        lambda model: _StubProvider(),
    )

    res = client.post(
        f"/research/copilot/sessions/{session_id}/bridge",
        json={"focus": "portfolio_risk", "depth": "brief", "target": "deepseek"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "markdown" in body["data"]
    assert body["data"]["event_id"]


def test_bridge_rate_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = "11111111-1111-1111-1111-111111111111"

    class _FakeConn:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "bifrost_research.copilot.bridge_runtime.connect",
        lambda: _FakeConn(),
    )
    monkeypatch.setattr(
        "bifrost_research.copilot.bridge_runtime.session_repo.get_session",
        lambda conn, sid, owner_id=None: {
            "id": sid,
            "messages": [{"kind": "text", "role": "user", "content": "hello world test"}],
        },
    )
    monkeypatch.setattr(
        "bifrost_research.copilot.bridge_runtime._resolve_chat_provider",
        lambda model: type(
            "P",
            (),
            {
                "complete": lambda self, *, messages, model: ProviderTurn(
                    text="ok", input_tokens=1, output_tokens=1, cost_usd=0.0
                )
            },
        )(),
    )
    monkeypatch.setattr(
        "bifrost_research.copilot.bridge_runtime.bridge_repo.insert_event",
        lambda conn, **kwargs: {"id": "x", "created_at": "2026-08-26T12:00:00+00:00"},
    )
    monkeypatch.setattr(
        "bifrost_research.copilot.bridge_runtime.session_repo.append_message",
        lambda *a, **k: None,
    )

    for _ in range(6):
        ok = client.post(
            f"/research/copilot/sessions/{session_id}/bridge",
            json={"focus": "portfolio_risk", "depth": "brief", "target": "generic"},
        )
        assert ok.status_code == 200

    blocked = client.post(
        f"/research/copilot/sessions/{session_id}/bridge",
        json={"focus": "portfolio_risk", "depth": "brief", "target": "generic"},
    )
    assert blocked.status_code == 429
