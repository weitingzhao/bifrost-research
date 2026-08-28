"""Copilot API tests — mocked LLM, no live API keys (Wave RS-E2.3)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.copilot.providers import ProviderTurn, ToolCallRequest, ToolSpec
from bifrost_research.copilot.rate_limit import record_usage, reset_usage_for_tests
from bifrost_research.mcp.server import TOOL_NAMES, create_mcp_server


class _ScriptedProvider:
    def __init__(self, turns: list[ProviderTurn]) -> None:
        self._turns = list(turns)
        self.seen_messages: list[list[dict[str, Any]]] = []

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        model: str,
    ) -> ProviderTurn:
        self.seen_messages.append(list(messages))
        if not self._turns:
            return ProviderTurn(text="(exhausted)", cost_usd=0.0)
        return self._turns.pop(0)


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"**Read-only**. Does not modify data. {name}"
        self.parameters: dict[str, Any] = {"type": "object", "properties": {}}


class _FakeToolManager:
    def list_tools(self) -> list[_FakeTool]:
        return [_FakeTool(n) for n in TOOL_NAMES]


class _FakeMcp:
    def __init__(self) -> None:
        self.called: list[tuple[str, dict[str, Any]]] = []
        self._tool_manager = _FakeToolManager()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.called.append((name, arguments))
        return {"ok": True, "data": {"rows": [{"id": "h1", "title": "NVDA skew"}], "count": 1}}


@pytest.fixture(autouse=True)
def _reset_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_usage_for_tests()
    # Avoid lifespan schema guard mutating global health status for other tests.
    monkeypatch.setattr(
        "bifrost_research.api.health.run_startup_schema_guard",
        lambda: None,
    )
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None
    yield
    health_mod._startup_ok = True
    health_mod._startup_error = None
    reset_usage_for_tests()


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    patched = _FakeMcp()
    app.state.copilot_mcp = patched
    app.state.copilot_provider = _ScriptedProvider(
        [
            ProviderTurn(
                text="",
                tool_calls=[
                    ToolCallRequest(
                        id="tc1",
                        name="research.hypothesis.list_active",
                        arguments={"symbol": "NVDA"},
                    )
                ],
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.0001,
            ),
            ProviderTurn(
                text="You have 1 active NVDA hypothesis: NVDA skew.",
                tool_calls=[],
                input_tokens=20,
                output_tokens=15,
                cost_usd=0.0002,
            ),
        ]
    )
    with TestClient(app) as c:
        c.app.state.fake_mcp = patched  # type: ignore[attr-defined]
        yield c


def _parse_sse(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in raw.split("\n\n"):
        line = block.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        events.append(json.loads(payload))
    return events


def test_usage_endpoint(client: TestClient) -> None:
    res = client.get("/research/copilot/usage")
    assert res.status_code == 200
    body = res.json()
    assert "tokens_today" in body
    assert "cap_usd" in body
    assert body["cap_usd"] >= 0
    assert "remaining_usd" in body


def test_stream_tool_loop(client: TestClient) -> None:
    res = client.post(
        "/research/copilot/stream",
        json={
            "messages": [{"role": "user", "content": "List active NVDA hypotheses"}],
            "model": "claude-4.5-sonnet",
            "max_tools": 8,
            "session_id": "test-1",
        },
    )
    assert res.status_code == 200
    assert "text/event-stream" in res.headers.get("content-type", "")
    events = _parse_sse(res.text)
    kinds = [e.get("event") for e in events]
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert "token" in kinds
    assert "done" in kinds
    tool_call = next(e for e in events if e.get("event") == "tool_call")
    assert tool_call["name"] == "research.hypothesis.list_active"
    fake = client.app.state.fake_mcp
    assert fake.called
    assert fake.called[0][0] == "research.hypothesis.list_active"


def test_chat_forces_write_tool_dry_run(client: TestClient) -> None:
    """RS-E4: chat may call write tools but orchestrator forces dry_run=true."""
    fake = client.app.state.fake_mcp

    async def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        fake.called.append((name, arguments))
        if name == "research.hypothesis.create":
            assert arguments.get("dry_run") is True
            assert "approval_token" not in arguments
            return {
                "ok": True,
                "data": {
                    "diff_kind": "create_hypothesis",
                    "preview": {"title": arguments.get("title")},
                    "impact": {"creates_row": True, "table": "research.hypothesis"},
                    "dry_run": True,
                },
            }
        return {"ok": True, "data": {}}

    fake.call_tool = _call  # type: ignore[method-assign]
    client.app.state.copilot_provider = _ScriptedProvider(
        [
            ProviderTurn(
                text="",
                tool_calls=[
                    ToolCallRequest(
                        id="w1",
                        name="research.hypothesis.create",
                        arguments={
                            "title": "x",
                            "thesis": "y",
                            "dry_run": False,
                            "approval_token": "evil",
                        },
                    )
                ],
            ),
            ProviderTurn(text="Here is a preview.", tool_calls=[]),
        ]
    )
    res = client.post(
        "/research/copilot/stream",
        json={"messages": [{"role": "user", "content": "create hyp"}]},
    )
    events = _parse_sse(res.text)
    results = [e for e in events if e.get("event") == "tool_result"]
    assert results
    assert results[0]["result"]["ok"] is True
    assert results[0]["result"]["data"]["dry_run"] is True
    assert fake.called
    assert fake.called[0][1].get("dry_run") is True


def test_rate_limit_429() -> None:
    reset_usage_for_tests()
    record_usage(tokens=1, cost_usd=99.0)
    app = create_app()
    app.state.copilot_provider = _ScriptedProvider([ProviderTurn(text="hi")])
    with TestClient(app) as c:
        res = c.post(
            "/research/copilot/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert res.status_code == 429
        assert "Retry-After" in res.headers


def test_stream_without_client_context_matches_legacy(client: TestClient) -> None:
    res = client.post(
        "/research/copilot/stream",
        json={
            "messages": [{"role": "user", "content": "List active NVDA hypotheses"}],
            "model": "claude-4.5-sonnet",
            "max_tools": 8,
            "session_id": "test-no-ctx",
        },
    )
    assert res.status_code == 200
    events = _parse_sse(res.text)
    assert "done" in [e.get("event") for e in events]
    provider = client.app.state.copilot_provider
    assert provider.seen_messages
    first = provider.seen_messages[0]
    assert not any(
        str(m.get("content", "")).startswith("Client view context:") for m in first
    )
    user_msgs = [m for m in first if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "List active NVDA hypotheses"


def test_stream_injects_client_context_system_message(client: TestClient) -> None:
    res = client.post(
        "/research/copilot/stream",
        json={
            "messages": [{"role": "user", "content": "What is VRP?"}],
            "model": "claude-4.5-sonnet",
            "client_context": {
                "origin_page": "vrp-lab",
                "origin_label": "VRP Lab",
                "symbol": "AAPL",
                "date": "2026-08-27",
                "panel": "term-structure",
                "snapshot": {
                    "vrp": 0.12,
                    "api_key": "sk-secret-should-not-leak",
                    "token": "abc-token",
                },
                "suggested_prompt": "Explain AAPL VRP",
            },
        },
    )
    assert res.status_code == 200
    provider = client.app.state.copilot_provider
    assert provider.seen_messages
    first = provider.seen_messages[0]
    ctx_msgs = [
        m
        for m in first
        if m.get("role") == "system"
        and str(m.get("content", "")).startswith("Client view context:")
    ]
    assert len(ctx_msgs) == 1
    content = ctx_msgs[0]["content"]
    assert content.startswith("Client view context: ")
    assert "origin=VRP Lab (vrp-lab)" in content
    assert "symbol=AAPL" in content
    assert "date=2026-08-27" in content
    assert "panel=term-structure" in content
    assert "snapshot=" in content
    assert '"vrp":0.12' in content
    assert "suggested_prompt=Explain AAPL VRP" in content
    assert "sk-secret-should-not-leak" not in content
    assert "abc-token" not in content
    user_msgs = [m for m in first if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "What is VRP?"
    assert "Client view context:" not in user_msgs[-1]["content"]


def test_stream_empty_client_context_is_noop(client: TestClient) -> None:
    res = client.post(
        "/research/copilot/stream",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "client_context": {},
        },
    )
    assert res.status_code == 200
    provider = client.app.state.copilot_provider
    first = provider.seen_messages[0]
    assert not any(
        str(m.get("content", "")).startswith("Client view context:") for m in first
    )


def test_missing_key_streams_error() -> None:
    reset_usage_for_tests()
    app = create_app()
    app.state.copilot_mcp = create_mcp_server()
    with TestClient(app) as c:
        res = c.post(
            "/research/copilot/stream",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "model": "claude-4.5-sonnet",
            },
        )
        assert res.status_code == 200
        events = _parse_sse(res.text)
        kinds = [e.get("event") for e in events]
        assert "error" in kinds
        assert "done" in kinds
