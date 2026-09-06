"""B2 (research-loop-automation) — two judges per candidate, agreement or dissent.

The judge seam is ``persona_judge._agent_verdicts_for_model_async``: one call
per model per symbol. Tests replace it with a fake that answers per model, so
the consensus, the caps, the budget and the ledger are exercised without a
model or a network.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bifrost_research.copilot import rate_limit
from bifrost_research.copilot.harness import persona_eval, persona_judge
from bifrost_research.copilot.harness.persona_heuristic import EVAL_AGENTS, _verdict_row
from bifrost_research.copilot.harness.persona_judge import DISSENT, consensus, most_severe


def _item(symbol: str = "AAPL", score: float = 82.0) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "score": score,
        "evidence": {
            "selection": {"sepa_score": 85, "path": "PIVOT", "status": "ok"},
            "track_record": {"horizons": [{"horizon_days": 20, "hit_rate": 0.6}]},
        },
    }


def _rows(model: str, verdict: str = "support", validate: str = "support") -> list[dict[str, Any]]:
    stances = {"analyze": "support", "portfolio": "abstain", "validate": validate, "verdict": verdict}
    return [
        _verdict_row(agent, stances[agent], f"{agent} by {model}", source="agent", model=model)
        for agent in EVAL_AGENTS
    ]


Behaviour = tuple[str, str] | Exception


def _install_judges(
    monkeypatch: pytest.MonkeyPatch,
    behaviour: dict[str, Behaviour],
    *,
    delay_s: float = 0.0,
) -> list[str]:
    """Replace the per-model judge with a scripted one; return the call log."""
    calls: list[str] = []

    async def fake(*, prompt: str, model_id: str, owner_id: str, mcp_url: str):
        calls.append(model_id)
        if delay_s:
            await asyncio.sleep(delay_s)
        call = persona_judge._new_call(model_id)
        b = behaviour[model_id]
        if isinstance(b, Exception):
            call["error"] = str(b)
            call["elapsed_ms"] = 5
            return [], call
        verdict, validate = b
        call.update(
            ok=True,
            elapsed_ms=120,
            input_tokens=1000,
            output_tokens=200,
            cost_usd=0.01 if model_id.startswith("gpt") else 0.001,
        )
        return _rows(model_id, verdict, validate), call

    monkeypatch.setattr(persona_judge, "_agent_verdicts_for_model_async", fake)
    return calls


@pytest.fixture(autouse=True)
def _agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_PERSONA_EVAL_AGENTS", "1")
    monkeypatch.delenv("BIFROST_PERSONA_EVAL_SKIP_AGENT", raising=False)
    monkeypatch.setenv("PERSONA_EVAL_MODELS", "deepseek-chat,gpt-4o-mini")
    monkeypatch.delenv("BIFROST_PERSONA_EVAL_TIMEOUT_S", raising=False)
    monkeypatch.setenv("PERSONA_EVAL_DAILY_CAP_USD_DEEPSEEK", "2.0")
    monkeypatch.setenv("PERSONA_EVAL_DAILY_CAP_USD_OPENAI", "2.0")
    # A URL keeps _judge_symbol from starting a local MCP server.
    monkeypatch.setenv("RESEARCH_MCP_SSE_URL", "http://mcp.test/sse")
    # No Trade monitor in tests: the portfolio heuristic abstains.
    monkeypatch.setattr(persona_eval, "load_held_symbols", lambda: (None, "unavailable"))
    rate_limit.reset_provider_usage_for_tests()


# --------------------------------------------------------------------------- #
# consensus — pure
# --------------------------------------------------------------------------- #


def test_consensus_agree_dissent_and_fallback() -> None:
    both = {"deepseek-chat": _rows("deepseek-chat"), "gpt-4o-mini": _rows("gpt-4o-mini")}
    c = consensus(both)
    assert (c["net_stance"], c["agreement"], c["validate_stance"]) == ("support", "agree", "support")

    split = {"deepseek-chat": _rows("deepseek-chat", "support"), "gpt-4o-mini": _rows("gpt-4o-mini", "caution")}
    c = consensus(split)
    assert (c["net_stance"], c["agreement"]) == (DISSENT, DISSENT)
    assert c["by_model"]["gpt-4o-mini"]["net"] == "caution"

    # Same words, but one judge is the heuristic standing in for a failed model.
    c = consensus(both, fallback_models={"gpt-4o-mini"})
    assert (c["net_stance"], c["agreement"]) == (DISSENT, DISSENT)


def test_consensus_validate_is_the_most_severe() -> None:
    rows = {
        "deepseek-chat": _rows("deepseek-chat", "support", "support"),
        "gpt-4o-mini": _rows("gpt-4o-mini", "support", "oppose"),
    }
    c = consensus(rows)
    assert c["agreement"] == "agree"
    assert c["net_stance"] == "support"
    assert c["validate_stance"] == "oppose"
    assert most_severe(["support", "abstain", "caution"]) == "caution"
    assert most_severe([]) == "abstain"


def test_consensus_single_judge() -> None:
    c = consensus({"deepseek-chat": _rows("deepseek-chat", "caution")})
    assert (c["net_stance"], c["agreement"]) == ("caution", "single")
    c = consensus({"deepseek-chat": _rows("deepseek-chat")}, fallback_models={"deepseek-chat"})
    assert (c["net_stance"], c["agreement"]) == (DISSENT, DISSENT)
    assert consensus({})["net_stance"] == "abstain"


# --------------------------------------------------------------------------- #
# evaluate_candidates — agent mode with scripted judges
# --------------------------------------------------------------------------- #


def test_two_judges_agree_is_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_judges(
        monkeypatch, {"deepseek-chat": ("support", "support"), "gpt-4o-mini": ("support", "support")}
    )
    items = [_item("AAPL"), _item("MSFT")]

    summary = persona_eval.evaluate_candidates(items, policy={"require_validate_pass": True})

    assert summary["mode"] == "agent"
    assert sorted(calls) == ["deepseek-chat", "deepseek-chat", "gpt-4o-mini", "gpt-4o-mini"]
    assert summary["agreement"] == {"agree": 2, "dissent": 0, "single": 0}
    assert summary["dissent_count"] == 0
    assert summary["auto_approve_eligible"] is True
    assert summary["fallback_used"] is False
    for item in items:
        assert item["net_stance"] == "support"
        assert item["agreement"] == "agree"
        verdicts = item["evidence"]["agent_verdicts"]
        assert len(verdicts) == 8
        assert {v["model"] for v in verdicts} == {"deepseek-chat", "gpt-4o-mini"}
        assert all(v["source"] == "agent" for v in verdicts)

    per = summary["per_symbol"][0]
    assert per["agreement"] == "agree"
    assert [m["model"] for m in per["models"]] == ["deepseek-chat", "gpt-4o-mini"]
    assert all(m["ok"] and not m["fallback"] for m in per["models"])
    assert per["models"][1]["cost_usd"] == 0.01

    by_model = {m["model"]: m for m in summary["models"]}
    assert by_model["gpt-4o-mini"]["provider"] == "openai"
    assert by_model["gpt-4o-mini"]["calls"] == 2
    assert by_model["gpt-4o-mini"]["cost_usd"] == pytest.approx(0.02)
    assert by_model["gpt-4o-mini"]["cap_usd"] == 2.0
    assert by_model["gpt-4o-mini"]["spent_today_usd"] == pytest.approx(0.02)
    assert by_model["deepseek-chat"]["input_tokens"] == 2000
    assert rate_limit.provider_usage("openai").remaining_usd == pytest.approx(1.98)


def test_disagreement_is_dissent_and_holds_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_judges(
        monkeypatch, {"deepseek-chat": ("support", "support"), "gpt-4o-mini": ("caution", "support")}
    )
    items = [_item("AAPL")]

    summary = persona_eval.evaluate_candidates(items, policy={"require_validate_pass": True})

    assert items[0]["net_stance"] == DISSENT
    assert items[0]["evidence"]["net_stance"] == DISSENT
    assert summary["dissent_count"] == 1
    assert summary["auto_approve_eligible"] is False
    assert summary["eligible_count"] == 0
    assert summary["blocked_by_validate"] == 0
    per = summary["per_symbol"][0]
    assert [m["net"] for m in per["models"]] == ["support", "caution"]


def test_failed_judge_counts_as_dissent(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_judges(
        monkeypatch,
        {"deepseek-chat": ("support", "support"), "gpt-4o-mini": TimeoutError("judge timed out")},
    )
    items = [_item("AAPL")]

    summary = persona_eval.evaluate_candidates(items, policy={"require_validate_pass": True})

    assert items[0]["net_stance"] == DISSENT
    assert summary["fallback_used"] is True
    assert summary["fallback_count"] == 1
    assert summary["auto_approve_eligible"] is False
    verdicts = items[0]["evidence"]["agent_verdicts"]
    gpt_rows = [v for v in verdicts if v.get("model") == "gpt-4o-mini"]
    assert len(gpt_rows) == 4
    assert all(v["source"] == "heuristic_fallback" for v in gpt_rows)
    assert all(v["agent_error"] == "judge timed out" for v in gpt_rows)
    per = summary["per_symbol"][0]
    gpt = next(m for m in per["models"] if m["model"] == "gpt-4o-mini")
    assert gpt["ok"] is False and gpt["fallback"] is True and gpt["error"] == "judge timed out"
    by_model = {m["model"]: m for m in summary["models"]}
    assert by_model["gpt-4o-mini"]["fallback"] == 1
    assert by_model["gpt-4o-mini"]["cost_usd"] == 0.0


def test_one_validate_oppose_blocks_even_when_verdicts_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_judges(
        monkeypatch, {"deepseek-chat": ("support", "support"), "gpt-4o-mini": ("support", "oppose")}
    )
    items = [_item("AAPL")]

    summary = persona_eval.evaluate_candidates(items, policy={"require_validate_pass": True})

    assert items[0]["agreement"] == "agree"
    assert items[0]["net_stance"] == "support"
    assert items[0]["blocked_by_validate"] is True
    assert summary["per_symbol"][0]["validate_stance"] == "oppose"
    assert summary["blocked_by_validate"] == 1
    assert summary["auto_approve_eligible"] is False


def test_provider_cap_exhausted_skips_that_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_EVAL_DAILY_CAP_USD_OPENAI", "0.50")
    rate_limit.seed_provider_cost("openai", 0.60)
    calls = _install_judges(
        monkeypatch, {"deepseek-chat": ("support", "support"), "gpt-4o-mini": ("support", "support")}
    )
    items = [_item("AAPL")]

    summary = persona_eval.evaluate_candidates(items, policy={"require_validate_pass": True})

    assert calls == ["deepseek-chat"]
    gpt = next(m for m in summary["per_symbol"][0]["models"] if m["model"] == "gpt-4o-mini")
    assert gpt["cap_exceeded"] is True and gpt["fallback"] is True
    assert "daily cap reached for openai" in gpt["error"]
    assert "PERSONA_EVAL_DAILY_CAP_USD_OPENAI=0.50" in gpt["error"]
    assert items[0]["net_stance"] == DISSENT
    assert summary["auto_approve_eligible"] is False
    by_model = {m["model"]: m for m in summary["models"]}
    assert by_model["gpt-4o-mini"]["cap_exceeded"] == 1
    assert by_model["gpt-4o-mini"]["cap_usd"] == 0.5


def test_budget_exhausted_falls_back_for_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_PERSONA_EVAL_TIMEOUT_S", "0.04")
    calls = _install_judges(
        monkeypatch,
        {"deepseek-chat": ("support", "support"), "gpt-4o-mini": ("support", "support")},
        delay_s=0.06,
    )
    items = [_item("AAPL"), _item("MSFT")]

    summary = persona_eval.evaluate_candidates(items, policy={"require_validate_pass": True})

    # The first symbol got both judges; the budget was gone before the second.
    assert sorted(calls) == ["deepseek-chat", "gpt-4o-mini"]
    assert summary["budget_s"] == 0.04
    assert summary["budget_exhausted_symbols"] == 1
    assert items[0]["net_stance"] == "support"
    assert items[1]["net_stance"] == DISSENT
    second = summary["per_symbol"][1]
    assert all(m["fallback"] and "eval budget exhausted" in m["error"] for m in second["models"])
    assert summary["auto_approve_eligible"] is False


def test_single_judge_is_single_not_agreement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_EVAL_MODELS", "deepseek-chat")
    _install_judges(monkeypatch, {"deepseek-chat": ("caution", "support")})
    items = [_item("AAPL")]

    summary = persona_eval.evaluate_candidates(items, policy={"require_validate_pass": True})

    assert items[0]["net_stance"] == "caution"
    assert items[0]["agreement"] == "single"
    assert summary["agreement"] == {"agree": 0, "dissent": 0, "single": 1}
    assert len(summary["models"]) == 1


class _FakeConn:
    def rollback(self) -> None:
        return None


def test_spend_ledger_is_seeded_and_written(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_judges(
        monkeypatch, {"deepseek-chat": ("support", "support"), "gpt-4o-mini": ("support", "support")}
    )
    monkeypatch.setattr(
        persona_judge.action_repo,
        "spend_today_by_provider",
        lambda conn, *, action_kind: {"openai": 1.5, "deepseek": 0.2},
    )
    written: list[dict[str, Any]] = []

    def _insert(conn, **kw):
        written.append(kw)
        return {"id": f"aal_{len(written)}"}

    monkeypatch.setattr(persona_judge.action_repo, "insert_action", _insert)
    items = [_item("AAPL")]

    summary = persona_eval.evaluate_candidates(
        items,
        policy={"require_validate_pass": True},
        conn=_FakeConn(),
        run_id="run_x",
        objective_id="obj-x",
    )

    # Seeded from the ledger, then this run's own calls on top.
    assert rate_limit.provider_usage("openai").cost_today_usd == pytest.approx(1.51)
    assert rate_limit.provider_usage("deepseek").cost_today_usd == pytest.approx(0.201)
    by_model = {m["model"]: m for m in summary["models"]}
    assert by_model["gpt-4o-mini"]["spent_before_run_usd"] == 1.5
    assert by_model["gpt-4o-mini"]["spent_today_usd"] == pytest.approx(1.51)

    assert summary["spend_rows_written"] == 2
    assert {w["action_kind"] for w in written} == {persona_judge.SPEND_ACTION_KIND}
    gpt = next(w for w in written if w["model"] == "gpt-4o-mini")
    assert gpt["provider"] == "openai"
    assert gpt["cost_usd"] == pytest.approx(0.01)
    assert gpt["status"] == "executed"
    assert gpt["input_payload"]["run_id"] == "run_x"
    assert gpt["input_payload"]["objective_id"] == "obj-x"
    assert gpt["output_payload"]["input_tokens"] == 1000


def test_ledger_failures_never_cost_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_judges(
        monkeypatch, {"deepseek-chat": ("support", "support"), "gpt-4o-mini": ("support", "support")}
    )

    def _boom(*a, **k):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(persona_judge.action_repo, "spend_today_by_provider", _boom)
    monkeypatch.setattr(persona_judge.action_repo, "insert_action", _boom)
    items = [_item("AAPL")]

    summary = persona_eval.evaluate_candidates(items, conn=_FakeConn(), run_id="r", objective_id="o")

    assert summary["status"] == "completed"
    assert summary["spend_rows_written"] == 0
    assert items[0]["net_stance"] == "support"


def test_heuristic_mode_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIFROST_PERSONA_EVAL_AGENTS", raising=False)
    calls = _install_judges(monkeypatch, {})
    items = [_item("AAPL")]

    summary = persona_eval.evaluate_candidates(items, policy={"require_validate_pass": True})

    assert calls == []
    assert summary["mode"] == "heuristic"
    assert summary["models"] == []
    assert summary["agreement"] == {"agree": 0, "dissent": 0, "single": 1}
    assert items[0]["agreement"] == "single"
    assert summary["per_symbol"][0]["models"] == []
    assert len(items[0]["evidence"]["agent_verdicts"]) == 4
    assert "model" not in items[0]["evidence"]["agent_verdicts"][0]


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


def test_eval_models_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert persona_eval.eval_models() == ["deepseek-chat", "gpt-4o-mini"]
    assert persona_eval.eval_models("gpt-4o") == ["gpt-4o"]
    monkeypatch.setenv("PERSONA_EVAL_MODELS", " gpt-4o-mini , deepseek-chat,gpt-4o-mini, ")
    assert persona_eval.eval_models() == ["gpt-4o-mini", "deepseek-chat"]
    monkeypatch.delenv("PERSONA_EVAL_MODELS")
    monkeypatch.setenv("BIFROST_PERSONA_EVAL_MODEL", "deepseek-reasoner")
    assert persona_eval.eval_models() == ["deepseek-reasoner"]
    assert persona_eval.provider_of("deepseek-chat") == "deepseek"
    assert persona_eval.provider_of("claude-sonnet") == "claude"


def test_eval_budget_reads_env_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_PERSONA_EVAL_TIMEOUT_S", "240")
    assert persona_eval.eval_budget_s() == 240.0
    monkeypatch.setenv("BIFROST_PERSONA_EVAL_TIMEOUT_S", "not a number")
    assert persona_eval.eval_budget_s() == persona_eval.DEFAULT_TIMEOUT_S


def test_provider_purses_are_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_EVAL_DAILY_CAP_USD_OPENAI", "1.0")
    monkeypatch.setenv("PERSONA_EVAL_DAILY_CAP_USD_DEEPSEEK", "bad")
    rate_limit.record_provider_usage("openai", tokens=100, cost_usd=0.9)
    rate_limit.record_provider_usage("deepseek", tokens=100, cost_usd=0.1)
    assert rate_limit.provider_usage("openai").remaining_usd == pytest.approx(0.1)
    assert rate_limit.provider_usage("deepseek").cap_usd == rate_limit.DEFAULT_PROVIDER_CAP_USD
    assert rate_limit.provider_usage("deepseek").remaining_usd == pytest.approx(1.9)
    # Seeding never erases what this process already counted.
    rate_limit.seed_provider_cost("openai", 0.5)
    assert rate_limit.provider_usage("openai").cost_today_usd == pytest.approx(0.9)
    rate_limit.seed_provider_cost("openai", 1.2)
    assert rate_limit.provider_remaining_usd("openai") == 0.0
    d = rate_limit.provider_usage_to_dict(rate_limit.provider_usage("openai"))
    assert d["provider"] == "openai" and d["tokens_today"] == 100


def test_estimate_cost_prices_the_small_openai_models_by_name() -> None:
    from bifrost_research.copilot.providers import estimate_cost

    mini = estimate_cost("gpt-4o-mini", 1_000_000, 1_000_000)
    full = estimate_cost("gpt-4o", 1_000_000, 1_000_000)
    assert mini == pytest.approx(0.75)
    assert full == pytest.approx(12.5)
    # 231k in / 9k out — the first DEV run — is cents, not two thirds of a dollar.
    assert estimate_cost("gpt-4o-mini", 231_056, 8_924) < 0.05


def test_a_partial_json_reply_is_a_failed_judgement(monkeypatch: pytest.MonkeyPatch) -> None:
    """gpt-4o-mini's first DEV reply was {"analyze": …} alone — not a verdict."""

    async def fake_run(*, prompt: str, model_id: str, owner_id: str, mcp_url: str):
        if model_id.startswith("gpt"):
            return (
                '```json\n{"analyze": {"stance": "caution", "summary": "only the analyst"}}\n```',
                {"input_tokens": 500, "output_tokens": 50},
            )
        full = {
            a: {"stance": "support", "summary": f"{a} ok"} for a in ("analyze", "portfolio", "validate", "verdict")
        }
        import json as _json

        return _json.dumps(full), {"input_tokens": 500, "output_tokens": 50}

    monkeypatch.setattr(persona_judge, "_run_verdict_agent_async", fake_run)
    items = [_item("NVDA")]

    summary = persona_eval.evaluate_candidates(items, policy={"require_validate_pass": True})

    per = summary["per_symbol"][0]
    gpt = next(m for m in per["models"] if m["model"] == "gpt-4o-mini")
    assert gpt["ok"] is False and gpt["fallback"] is True
    assert gpt["error"] == "incomplete JSON (missing: portfolio, validate, verdict)"
    # Tokens were still spent and are still counted.
    assert gpt["cost_usd"] > 0
    ds = next(m for m in per["models"] if m["model"] == "deepseek-chat")
    assert ds["ok"] is True and ds["net"] == "support"
    assert items[0]["net_stance"] == DISSENT


def test_a_judge_timeout_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(*, prompt: str, model_id: str, owner_id: str, mcp_url: str):
        if model_id.startswith("gpt"):
            raise TimeoutError()
        raise RuntimeError("")

    monkeypatch.setattr(persona_judge, "_run_verdict_agent_async", fake_run)
    items = [_item("NVDA")]

    summary = persona_eval.evaluate_candidates(items, policy={"require_validate_pass": True})

    errors = {m["model"]: m["error"] for m in summary["per_symbol"][0]["models"]}
    assert errors["gpt-4o-mini"] == f"timeout after {persona_judge.PER_SYMBOL_TIMEOUT_S:.0f}s"
    assert errors["deepseek-chat"] == "RuntimeError"
