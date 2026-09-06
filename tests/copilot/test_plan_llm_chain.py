"""B1 (research-loop-automation) — the plan model chain.

DeepSeek chat → OpenAI gpt-4o-mini → heuristic, every hop recorded. httpx is
faked per host so a test can time out one provider and answer from the other.
"""

from __future__ import annotations

import json
from typing import Any, Self

import httpx
import pytest

from bifrost_research.copilot.harness import plan_llm
from bifrost_research.copilot.harness.planning import _plan_for_objective

VALID_PLAN = json.dumps(
    {
        "steps": [
            {"op": "scan_universe", "note": "scan"},
            {"op": "analyze_symbol", "note": "evidence"},
            {"op": "propose_candidates", "note": "propose"},
            {"op": "await_approval", "note": "owner"},
        ],
        "reasoning": "Narrow universe; decay check not needed.",
    }
)


def _chat_response(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 800, "completion_tokens": 120},
    }


class _Reply:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("non-json body")
        return self._payload


class _RoutingClient:
    """httpx.Client stand-in that answers per host: a reply, or an exception."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self._routes = routes
        self.calls: list[dict[str, Any]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, url: str, *, json: Any, headers: dict[str, Any]) -> _Reply:
        self.calls.append({"url": url, "body": json, "headers": headers})
        for host, outcome in self._routes.items():
            if host in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"unexpected url {url}")


def _install(monkeypatch: pytest.MonkeyPatch, client: _RoutingClient) -> None:
    monkeypatch.setattr(plan_llm.httpx, "Client", lambda *a, **k: client)


def _obj(**policy: Any) -> dict[str, Any]:
    return {
        "id": "obj-chain",
        "title": "Daily stock loop",
        "description": "Stock-first candidates.",
        "persona": "loop_curator",
        "schedule": "daily_open",
        "policy_json": {"max_candidates": 3, **policy},
    }


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_HARNESS_LLM_PLAN", "1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


def test_chain_order_is_policy_then_defaults() -> None:
    assert plan_llm.model_chain({}) == ["deepseek-chat", "gpt-4o-mini"]
    assert plan_llm.model_chain({"llm_model": "gpt-4o-mini"}) == ["gpt-4o-mini", "deepseek-chat"]
    assert plan_llm.model_chain({"llm_model": "deepseek-reasoner"}) == [
        "deepseek-reasoner",
        "deepseek-chat",
        "gpt-4o-mini",
    ]


def test_defaults_are_the_repaired_ones() -> None:
    assert plan_llm.DEFAULT_MODEL == "deepseek-chat"
    assert plan_llm.FALLBACK_MODEL == "gpt-4o-mini"
    assert plan_llm.DEFAULT_TIMEOUT_SECONDS == 60.0


def test_deepseek_timeout_falls_through_to_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _RoutingClient(
        {
            "api.deepseek.com": httpx.ReadTimeout("slow"),
            "api.openai.com": _Reply(200, _chat_response(VALID_PLAN)),
        }
    )
    _install(monkeypatch, client)

    result, attempts = plan_llm.plan_with_chain(_obj(), timeout_seconds=60.0)

    assert result is not None
    assert result["llm_model"] == "gpt-4o-mini"
    assert result["llm_provider"] == "openai"
    assert result["steps"][0]["op"] == "scan_universe"

    assert [a["model"] for a in attempts] == ["deepseek-chat", "gpt-4o-mini"]
    assert attempts[0]["ok"] is False
    assert attempts[0]["error"] == "timeout after 60s"
    assert attempts[1]["ok"] is True
    assert attempts[1]["http_status"] == 200
    assert attempts[1]["input_tokens"] == 800
    assert attempts[1]["cost_usd"] > 0
    assert result["attempts"] is attempts

    # Both hops asked for json mode; each carried its own provider's key.
    assert [c["body"]["response_format"] for c in client.calls] == [{"type": "json_object"}] * 2
    assert client.calls[0]["headers"]["Authorization"] == "Bearer sk-deepseek"
    assert client.calls[1]["url"] == "https://api.openai.com/v1/chat/completions"
    assert client.calls[1]["headers"]["Authorization"] == "Bearer sk-openai"
    assert client.calls[1]["body"]["model"] == "gpt-4o-mini"


def test_invalid_json_from_deepseek_is_a_named_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _RoutingClient(
        {
            "api.deepseek.com": _Reply(200, _chat_response('{"steps": []}')),
            "api.openai.com": _Reply(200, _chat_response(VALID_PLAN)),
        }
    )
    _install(monkeypatch, client)

    result, attempts = plan_llm.plan_with_chain(_obj())

    assert result is not None and result["llm_provider"] == "openai"
    assert attempts[0]["error"].startswith("schema violation")


def test_both_fail_yields_heuristic_with_every_hop_named(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _RoutingClient(
        {
            "api.deepseek.com": httpx.ReadTimeout("slow"),
            "api.openai.com": _Reply(500, {"error": "boom"}),
        }
    )
    _install(monkeypatch, client)

    plan = _plan_for_objective(_obj(), conn=None)

    assert plan["generated_by"] == "heuristic"
    assert plan["fallback_reason"] == (
        "llm_failed: deepseek-chat@deepseek timeout after 60s; gpt-4o-mini@openai HTTP 500"
    )
    assert [a["ok"] for a in plan["llm_attempts"]] == [False, False]
    assert plan["llm_attempts"][1]["http_status"] == 500
    ops = [s["op"] for s in plan["steps"]]
    assert "propose_candidates" in ops and "await_approval" in ops


def test_missing_openai_key_is_recorded_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _RoutingClient({"api.deepseek.com": httpx.ConnectError("no route")})
    _install(monkeypatch, client)

    result, attempts = plan_llm.plan_with_chain(_obj())

    assert result is None
    assert [a["model"] for a in attempts] == ["deepseek-chat", "gpt-4o-mini"]
    assert attempts[1]["error"] == "OPENAI_API_KEY not configured"
    assert attempts[1]["elapsed_ms"] == 0
    # Only DeepSeek was actually called.
    assert len(client.calls) == 1
    assert plan_llm.failure_reason(attempts) == (
        "llm_failed: deepseek-chat@deepseek httpx error no route; "
        "gpt-4o-mini@openai OPENAI_API_KEY not configured"
    )


def test_policy_model_leads_the_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _RoutingClient({"api.openai.com": _Reply(200, _chat_response(VALID_PLAN))})
    _install(monkeypatch, client)

    plan = _plan_for_objective(_obj(llm_model="gpt-4o-mini", use_llm_plan=True), conn=None)

    assert plan["generated_by"] == "llm"
    assert plan["llm_model"] == "gpt-4o-mini"
    assert plan["llm_provider"] == "openai"
    assert len(plan["llm_attempts"]) == 1
    assert "fallback_reason" not in plan
    assert len(client.calls) == 1


def test_unknown_model_id_is_skipped_with_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _RoutingClient({"api.deepseek.com": _Reply(200, _chat_response(VALID_PLAN))})
    _install(monkeypatch, client)

    result, attempts = plan_llm.plan_with_chain(_obj(), models=["claude-sonnet", "deepseek-chat"])

    assert result is not None and result["llm_model"] == "deepseek-chat"
    assert attempts[0]["provider"] == "unknown"
    assert "No chat-completions endpoint" in attempts[0]["error"]


def test_disabled_makes_no_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIFROST_HARNESS_LLM_PLAN", raising=False)
    client = _RoutingClient({})
    _install(monkeypatch, client)

    assert plan_llm.plan_with_chain(_obj()) == (None, [])
    assert client.calls == []
    assert plan_llm.failure_reason([]) == "llm_failed: no model attempted"
