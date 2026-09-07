"""Wave 1–5 harness persona / discovery / suggestion contracts."""

from __future__ import annotations

from bifrost_research.copilot.harness.discovery_assist import apply_discovery_assist
from bifrost_research.copilot.harness.persona_eval import (
    evaluate_candidates,
    heuristic_verdicts_for_item,
    net_stance_from_verdicts,
)
from bifrost_research.copilot.harness.report import compose_report
from bifrost_research.copilot.harness.suggestion import policy_suggestion_from_outcomes


def test_heuristic_verdicts_shape():
    item = {
        "symbol": "AAPL",
        "score": 82.0,
        "evidence": {
            "selection": {"sepa_score": 85, "path": "PIVOT", "status": "ok"},
            "track_record": {
                "horizons": [{"horizon_days": 20, "hit_rate": 0.6}],
            },
        },
    }
    verdicts = heuristic_verdicts_for_item(item)
    assert [v["agent"] for v in verdicts] == ["analyze", "portfolio", "validate", "verdict"]
    assert all(v["stance"] in {"support", "caution", "oppose", "abstain"} for v in verdicts)
    assert net_stance_from_verdicts(verdicts) in {"support", "caution", "oppose", "abstain"}


def test_evaluate_candidates_blocks_on_validate_oppose():
    items = [
        {
            "symbol": "WEAK",
            "score": 40.0,
            "evidence": {
                "selection": {"sepa_score": 40, "status": "ok"},
                "track_record": {
                    "horizons": [{"horizon_days": 20, "hit_rate": 0.1}],
                },
            },
        }
    ]
    summary = evaluate_candidates(items, policy={"require_validate_pass": True})
    assert summary["symbols_evaluated"] == 1
    assert summary["mode"] == "heuristic"
    assert items[0]["blocked_by_validate"] is True
    assert items[0]["evidence"]["agent_verdicts"]
    assert summary["auto_approve_eligible"] is False


def test_compose_report_includes_net_stance():
    items = [
        {
            "symbol": "AAPL",
            "score": 80,
            "net_stance": "support",
            "blocked_by_validate": False,
            "evidence": {
                "selection": {"path": "SETUP", "sepa_score": 80},
                "agent_verdicts": [
                    {"agent": "analyze", "stance": "support", "summary": "ok"},
                    {"agent": "portfolio", "stance": "abstain", "summary": "n/a"},
                    {"agent": "validate", "stance": "caution", "summary": "mixed"},
                    {"agent": "verdict", "stance": "caution", "summary": "net caution"},
                ],
                "invalidation": ["breaks 50dma"],
            },
        }
    ]
    report = compose_report(
        objective={"id": "obj-1", "title": "t"},
        run_id="run-1",
        items=items,
        funnel=[],
        backtest=None,
    )
    assert report["candidates"][0]["net_stance"] == "support"
    assert "net_stance_counts" in report["coverage"]
    assert report["candidates"][0]["agent_verdicts"]["validate"]["stance"] == "caution"


def test_discovery_assist_disabled_passthrough():
    out = apply_discovery_assist(["AAPL", "MSFT"], policy={"discovery_assist": {"enabled": False}})
    assert out["symbols"] == ["AAPL", "MSFT"]
    assert out["funnel_step"]["skipped"] is True


def test_discovery_assist_veto():
    rules = [{"title": "Avoid XYZ forever", "body": "veto XYZ names"}]
    out = apply_discovery_assist(
        ["AAPL", "XYZ", "MSFT"],
        policy={"discovery_assist": {"enabled": True, "max_veto_fraction": 0.5}},
        playbook_rules=rules,
    )
    assert "XYZ" in out["veto"]
    assert "XYZ" not in out["symbols"]


def test_heuristic_portfolio_with_holdings():
    item = {
        "symbol": "AAPL",
        "score": 80.0,
        "evidence": {
            "selection": {"sepa_score": 82, "path": "SETUP", "status": "ok"},
            "track_record": {"horizons": [{"horizon_days": 20, "hit_rate": 0.5}]},
        },
    }
    held = heuristic_verdicts_for_item(
        item, held_symbols={"AAPL", "MSFT"}, holdings_status="applied"
    )
    port = next(v for v in held if v["agent"] == "portfolio")
    assert port["stance"] == "caution"
    assert "Already held" in port["summary"]

    fresh = heuristic_verdicts_for_item(
        {**item, "symbol": "NVDA"},
        held_symbols={"AAPL"},
        holdings_status="applied",
    )
    port2 = next(v for v in fresh if v["agent"] == "portfolio")
    assert port2["stance"] == "support"

    missing = heuristic_verdicts_for_item(item, held_symbols=None, holdings_status="unavailable")
    port3 = next(v for v in missing if v["agent"] == "portfolio")
    assert port3["stance"] == "abstain"
    assert "Holdings not applied" in port3["summary"]


def test_policy_suggestion_from_outcomes_when_majority_blocked():
    persona_eval = {
        "symbols_evaluated": 4,
        "blocked_by_validate": 3,
    }
    current = {
        "universe_mode": "stock_composite",
        "layers": {"sepa": {"min_score": 70, "stage": ["SETUP"]}},
    }
    sug = policy_suggestion_from_outcomes(persona_eval, current_policy=current)
    assert sug is not None
    assert "layers" in sug["suggestion"]
    assert sug["suggestion"]["layers"]["sepa"]["min_score"] == 75.0


def test_policy_suggestion_from_weak_candidate_outcomes():
    persona_eval = {"symbols_evaluated": 1, "blocked_by_validate": 0}
    current = {
        "universe_mode": "stock_composite",
        "max_candidates": 8,
        "layers": {"sepa": {"min_score": 70}},
    }
    outcome = {
        "horizons": [
            {"horizon_days": 20, "hit_rate": 0.25, "judged": 12, "settled": 12},
        ]
    }
    sug = policy_suggestion_from_outcomes(
        persona_eval, current_policy=current, outcome_summary=outcome
    )
    assert sug is not None
    assert sug["suggestion"]["layers"]["sepa"]["min_score"] == 78.0
    assert sug["suggestion"]["max_candidates"] == 7
    assert "candidate_outcome" in sug["reasoning"]


# --- persona_stage: the judge stage records itself (extracted from runtime) ---


def _stage_args(**over):
    args = {
        "policy": {},
        "owner_id": "owner",
        "conn": None,
        "run_id": "run_x",
        "objective_id": "obj_x",
        "trace": [],
    }
    args.update(over)
    return args


def test_persona_stage_returns_none_only_when_the_judges_were_never_asked(monkeypatch):
    from bifrost_research.copilot.harness import persona_eval as mod

    # No items: nobody was asked. None is the honest answer, and it is the only
    # state the run may treat as auto-approvable-by-default.
    trace: list[dict] = []
    assert mod.persona_stage([], **_stage_args(trace=trace)) is None
    assert trace == []


def test_persona_stage_reports_a_failure_as_a_summary_not_as_silence(monkeypatch):
    from bifrost_research.copilot.harness import persona_eval as mod

    def boom(*_a, **_kw):
        raise RuntimeError("judge exploded")

    monkeypatch.setattr(mod, "evaluate_candidates", boom)
    trace: list[dict] = []
    flushed: list[dict] = []
    errors: list[BaseException] = []
    out = mod.persona_stage(
        [{"symbol": "AAA"}],
        **_stage_args(trace=trace, flush=lambda **kw: flushed.append(kw), on_error=errors.append),
    )
    # A stage that blew up must be distinguishable from a stage never run: the
    # first is not eligible for auto-approve, the second is.
    assert out == {"status": "error", "error": "judge exploded"}
    assert trace[0]["step"] == "persona_evaluate" and trace[0]["decision"] == "error"
    assert flushed[0]["step"] == "persona_evaluate"
    assert isinstance(errors[0], RuntimeError)


def test_persona_stage_traces_the_decision_line_it_shows_the_owner(monkeypatch):
    from bifrost_research.copilot.harness import persona_eval as mod

    summary = {
        "mode": "agent",
        "models": [{"model": "a"}, {"model": "b"}],
        "agreement": {"agree": 1, "dissent": 2},
        "blocked_by_validate": 3,
        "auto_approve_eligible": False,
    }
    monkeypatch.setattr(mod, "evaluate_candidates", lambda *_a, **_kw: dict(summary))
    trace: list[dict] = []
    out = mod.persona_stage([{"symbol": "AAA"}], **_stage_args(trace=trace))
    assert out["mode"] == "agent"
    line = trace[0]["decision"]
    assert "models=2 agree=1 dissent=2" in line
    assert "blocked_by_validate=3" in line
    assert "auto_approve_eligible=False" in line


def test_decision_line_omits_the_model_clause_for_a_heuristic_run():
    from bifrost_research.copilot.harness.persona_eval import persona_decision_line

    line = persona_decision_line({"mode": "heuristic", "blocked_by_validate": 0})
    assert "models=" not in line
    assert line.startswith("mode=heuristic")


def test_verdict_row_carries_the_chinese_only_when_a_judge_wrote_one():
    from bifrost_research.copilot.harness.persona_heuristic import _verdict_row

    both = _verdict_row("analyze", "support", "English", summary_zh="中文", source="agent")
    assert both["summary_zh"] == "中文"
    # Heuristic rows and every run made before the judges were asked for Chinese
    # leave the key off entirely, so the reader falls back rather than seeing a
    # blank field dressed as a translation.
    assert "summary_zh" not in _verdict_row("analyze", "support", "English")
    assert "summary_zh" not in _verdict_row("analyze", "support", "English", summary_zh="   ")


def test_judge_prompt_asks_for_both_languages_in_the_same_reply():
    from bifrost_research.copilot.harness.persona_judge import _eval_prompt

    prompt = _eval_prompt({"symbol": "AAA", "evidence": {}})
    assert '"summary_zh"' in prompt
    assert "Simplified Chinese" in prompt
    # The bill is input tokens, so asking once for both is far cheaper than a
    # second call; the sentence must be the same claim, not a looser paraphrase.
    assert "not a looser paraphrase" in prompt
