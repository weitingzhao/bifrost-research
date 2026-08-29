"""Wave Y.2 — harness LLM plan generation (fail-soft matrix)."""

from __future__ import annotations

import json
from typing import Any, Self

import httpx
import pytest

from bifrost_research.copilot.harness import plan_llm

# --------------------------------------------------------------------------- #
# is_llm_plan_enabled matrix
# --------------------------------------------------------------------------- #


class TestIsLlmPlanEnabled:
    def test_env_off_policy_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BIFROST_HARNESS_LLM_PLAN", raising=False)
        enabled, reason = plan_llm.is_llm_plan_enabled({})
        assert enabled is False
        assert "env off" in reason

    def test_env_on_policy_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BIFROST_HARNESS_LLM_PLAN", "1")
        enabled, reason = plan_llm.is_llm_plan_enabled({})
        assert enabled is True
        assert "env" in reason

    def test_policy_true_overrides_env_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BIFROST_HARNESS_LLM_PLAN", raising=False)
        enabled, reason = plan_llm.is_llm_plan_enabled({"use_llm_plan": True})
        assert enabled is True
        assert "policy" in reason

    def test_policy_false_overrides_env_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BIFROST_HARNESS_LLM_PLAN", "1")
        enabled, reason = plan_llm.is_llm_plan_enabled({"use_llm_plan": False})
        assert enabled is False
        assert "policy" in reason

    def test_env_truthy_variants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for value in ["1", "true", "TRUE", "yes", "on"]:
            monkeypatch.setenv("BIFROST_HARNESS_LLM_PLAN", value)
            enabled, _ = plan_llm.is_llm_plan_enabled(None)
            assert enabled is True, f"value={value!r} should enable"

    def test_env_falsy_variants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for value in ["0", "false", "no", "off", "", " "]:
            monkeypatch.setenv("BIFROST_HARNESS_LLM_PLAN", value)
            enabled, _ = plan_llm.is_llm_plan_enabled(None)
            assert enabled is False, f"value={value!r} should disable"


# --------------------------------------------------------------------------- #
# _parse_llm_json — variants
# --------------------------------------------------------------------------- #


class TestParseLlmJson:
    def test_plain_json(self) -> None:
        assert plan_llm._parse_llm_json('{"a": 1}') == {"a": 1}

    def test_code_fenced_json(self) -> None:
        wrapped = '```json\n{"steps": [{"op": "await_approval"}]}\n```'
        parsed = plan_llm._parse_llm_json(wrapped)
        assert parsed == {"steps": [{"op": "await_approval"}]}

    def test_leading_prose(self) -> None:
        raw = "Here is the plan you asked for:\n{\"x\": 42} — thank you"
        assert plan_llm._parse_llm_json(raw) == {"x": 42}

    def test_empty_returns_none(self) -> None:
        assert plan_llm._parse_llm_json("") is None
        assert plan_llm._parse_llm_json("   ") is None

    def test_non_object_returns_none(self) -> None:
        assert plan_llm._parse_llm_json("[1, 2, 3]") is None

    def test_malformed_returns_none(self) -> None:
        assert plan_llm._parse_llm_json("{not json") is None


# --------------------------------------------------------------------------- #
# generate_plan_llm — fail-soft matrix
# --------------------------------------------------------------------------- #


def _valid_llm_response(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("non-json body")
        return self._payload


class _FakeClient:
    """Context-manager compatible stand-in for httpx.Client."""

    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        raise_on_post: Exception | None = None,
    ) -> None:
        self._response = response
        self._raise = raise_on_post
        self.captured_url: str | None = None
        self.captured_body: Any = None
        self.captured_headers: dict[str, Any] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, url: str, *, json: Any, headers: dict[str, Any]) -> _FakeResponse:
        self.captured_url = url
        self.captured_body = json
        self.captured_headers = headers
        if self._raise:
            raise self._raise
        assert self._response is not None
        return self._response


@pytest.fixture(autouse=True)
def _env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with LLM plan enabled + API key present.

    Individual tests override as needed.
    """
    monkeypatch.setenv("BIFROST_HARNESS_LLM_PLAN", "1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-123")


def _install_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(
        plan_llm.httpx,
        "Client",
        lambda *a, **k: fake,
    )


VALID_PLAN_JSON = json.dumps(
    {
        "steps": [
            {"op": "scan_universe", "note": "top 5 hot IVR"},
            {"op": "propose_candidates", "note": "propose"},
            {"op": "await_approval", "note": "owner"},
        ],
        "reasoning": "Skipping decay check because objective is narrow.",
        "policy_suggestion": {"min_composite_score": 70.0},
    }
)


def _obj(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "obj-a",
        "title": "Test Objective",
        "description": "Screen momentum names for iron condor.",
        "persona": "loop_curator",
        "schedule": "manual",
        "policy_json": {"max_candidates": 3},
    }
    base.update(overrides)
    return base


def test_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIFROST_HARNESS_LLM_PLAN", raising=False)
    result = plan_llm.generate_plan_llm(_obj())
    assert result is None


def test_policy_disable_returns_none() -> None:
    result = plan_llm.generate_plan_llm(_obj(policy_json={"use_llm_plan": False}))
    assert result is None


def test_missing_api_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = plan_llm.generate_plan_llm(_obj())
    assert result is None


def test_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(response=_FakeResponse(200, _valid_llm_response(VALID_PLAN_JSON)))
    _install_client(monkeypatch, fake)

    result = plan_llm.generate_plan_llm(_obj())
    assert result is not None
    assert result["llm_model"] == plan_llm.DEFAULT_MODEL
    assert [s["op"] for s in result["steps"]] == [
        "scan_universe",
        "propose_candidates",
        "await_approval",
    ]
    assert result["reasoning"].startswith("Skipping")
    assert result["policy_suggestion"] == {"min_composite_score": 70.0}

    # Prompt shape
    body = fake.captured_body
    assert body["model"] == plan_llm.DEFAULT_MODEL
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    assert "OBJECTIVE" in body["messages"][1]["content"]
    assert fake.captured_headers["Authorization"] == "Bearer sk-test-123"
    assert fake.captured_url.endswith("/chat/completions")


def test_policy_overrides_model(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(response=_FakeResponse(200, _valid_llm_response(VALID_PLAN_JSON)))
    _install_client(monkeypatch, fake)

    result = plan_llm.generate_plan_llm(
        _obj(policy_json={"llm_model": "deepseek-chat", "max_candidates": 3})
    )
    assert result is not None
    assert result["llm_model"] == "deepseek-chat"
    assert fake.captured_body["model"] == "deepseek-chat"


def test_env_base_url_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://alt-endpoint.example.com/v1/")
    fake = _FakeClient(response=_FakeResponse(200, _valid_llm_response(VALID_PLAN_JSON)))
    _install_client(monkeypatch, fake)

    result = plan_llm.generate_plan_llm(_obj())
    assert result is not None
    assert fake.captured_url == "https://alt-endpoint.example.com/v1/chat/completions"


def test_timeout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(raise_on_post=httpx.ReadTimeout("slow"))
    _install_client(monkeypatch, fake)
    assert plan_llm.generate_plan_llm(_obj()) is None


def test_http_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(raise_on_post=httpx.ConnectError("no route"))
    _install_client(monkeypatch, fake)
    assert plan_llm.generate_plan_llm(_obj()) is None


def test_http_4xx_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(response=_FakeResponse(401, {"error": "unauthorized"}))
    _install_client(monkeypatch, fake)
    assert plan_llm.generate_plan_llm(_obj()) is None


def test_non_json_body_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(response=_FakeResponse(200, None, text="<html>bad</html>"))
    _install_client(monkeypatch, fake)
    assert plan_llm.generate_plan_llm(_obj()) is None


def test_empty_choices_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(response=_FakeResponse(200, {"choices": []}))
    _install_client(monkeypatch, fake)
    assert plan_llm.generate_plan_llm(_obj()) is None


def test_content_unparseable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        response=_FakeResponse(200, _valid_llm_response("this is not json at all"))
    )
    _install_client(monkeypatch, fake)
    assert plan_llm.generate_plan_llm(_obj()) is None


def test_schema_missing_steps_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = json.dumps({"reasoning": "no steps here"})
    fake = _FakeClient(response=_FakeResponse(200, _valid_llm_response(bad)))
    _install_client(monkeypatch, fake)
    assert plan_llm.generate_plan_llm(_obj()) is None


def test_schema_empty_steps_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = json.dumps({"steps": []})
    fake = _FakeClient(response=_FakeResponse(200, _valid_llm_response(bad)))
    _install_client(monkeypatch, fake)
    assert plan_llm.generate_plan_llm(_obj()) is None


def test_op_not_in_whitelist_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = json.dumps({"steps": [{"op": "place_order"}, {"op": "await_approval"}]})
    fake = _FakeClient(response=_FakeResponse(200, _valid_llm_response(bad)))
    _install_client(monkeypatch, fake)
    assert plan_llm.generate_plan_llm(_obj()) is None


def test_fenced_content_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    fenced = f"```json\n{VALID_PLAN_JSON}\n```"
    fake = _FakeClient(response=_FakeResponse(200, _valid_llm_response(fenced)))
    _install_client(monkeypatch, fake)
    result = plan_llm.generate_plan_llm(_obj())
    assert result is not None
    assert len(result["steps"]) == 3


# --------------------------------------------------------------------------- #
# Y.3 — policy_suggestion whitelist enforcement
# --------------------------------------------------------------------------- #


def test_policy_suggestion_out_of_whitelist_keys_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Y.3: LLM may not propose fields the runtime does not honor."""
    raw = json.dumps(
        {
            "steps": [
                {"op": "propose_candidates", "note": "test"},
                {"op": "await_approval", "note": "test"},
            ],
            "policy_suggestion": {
                "min_hit_rate": 0.7,
                "arbitrary_new_field": "x",
                "seed_symbols": ["AAPL"],  # not in whitelist
            },
        }
    )
    fake = _FakeClient(response=_FakeResponse(200, _valid_llm_response(raw)))
    _install_client(monkeypatch, fake)
    result = plan_llm.generate_plan_llm(_obj())
    assert result is not None
    # only whitelist key survived
    assert result["policy_suggestion"] == {"min_hit_rate": 0.7}


def test_policy_suggestion_only_out_of_whitelist_becomes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.dumps(
        {
            "steps": [
                {"op": "propose_candidates", "note": "test"},
                {"op": "await_approval", "note": "test"},
            ],
            "policy_suggestion": {"seed_symbols": ["X"], "foo": 1},
        }
    )
    fake = _FakeClient(response=_FakeResponse(200, _valid_llm_response(raw)))
    _install_client(monkeypatch, fake)
    result = plan_llm.generate_plan_llm(_obj())
    assert result is not None
    assert result["policy_suggestion"] is None


def test_policy_suggestion_non_object_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.dumps(
        {
            "steps": [
                {"op": "propose_candidates", "note": "test"},
                {"op": "await_approval", "note": "test"},
            ],
            "policy_suggestion": "should be an object",
        }
    )
    fake = _FakeClient(response=_FakeResponse(200, _valid_llm_response(raw)))
    _install_client(monkeypatch, fake)
    # Pydantic ValidationError → fail-soft None
    assert plan_llm.generate_plan_llm(_obj()) is None
