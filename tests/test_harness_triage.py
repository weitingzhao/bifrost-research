"""Triage — the cheap pass that ranks candidates before the judges read them."""

from __future__ import annotations

import json

from bifrost_research.copilot.harness.triage import (
    DEFAULT_TRIAGE_MODEL,
    _parse_ranked,
    compact_item,
    deep_judge_top_n,
    heuristic_ranked,
    split_for_deep,
    triage_decision,
    triage_enabled,
    triage_messages,
    triage_model,
)

ITEM = {
    "symbol": "bg",
    "score": 34.79,
    "evidence": {
        "symbol": "BG",
        "selection": {
            "path": "AVOID",
            "grade": "D",
            "stage": "STAGE_4",
            "status": "ok",
            "sepa_score": 34.7903,
            "components": {"momentum": 0.0, "structure": 75.8},
            "checks_passed": {"technical": 5},
        },
        "price_context": {"close": 63.81, "sma_50": 75.244, "sma_200": 79.37, "pct_off_52w_high": -57.74, "low_52w": 37.57},
        "option_analytics": {"status": "ok", "iv_rank_1y": 26.752, "terrain_regime": "range", "total_net_gex": 6144300.6},
        "track_record": {"status": "ok", "horizons": [{"horizon_days": 1, "judged": 8, "hit_rate": 0.5, "avg_excess": 0.0086}]},
        "agent_verdicts": [{"agent": "analyze", "summary": "long text " * 50}],
        "invalidation": ["path leaves AVOID"],
    },
}


def test_compact_item_keeps_the_decisive_facts_and_drops_the_bulk():
    out = compact_item(ITEM)
    assert out["symbol"] == "BG"
    assert out["selection"] == {"path": "AVOID", "grade": "D", "stage": "STAGE_4", "sepa_score": 34.7903, "status": "ok"}
    assert out["settled_record"] == {"horizon_days": 1, "judged": 8, "hit_rate": 0.5}
    assert out["options"]["terrain_regime"] == "range"
    # The judges get the full evidence. Handing triage the same thing would make
    # the cheap stage cost what the expensive one does.
    assert "agent_verdicts" not in json.dumps(out)
    assert "components" not in json.dumps(out)
    assert len(json.dumps(out)) < 600


def test_compact_item_survives_a_candidate_with_no_evidence():
    assert compact_item({"symbol": "EE"}) == {"symbol": "EE"}
    assert compact_item({"symbol": "EE", "evidence": {"track_record": {"horizons": []}}}) == {"symbol": "EE"}


def test_prompt_states_the_price_of_the_stage_it_is_gating():
    msgs = triage_messages([ITEM])
    system = msgs[0]["content"]
    assert "$0.077 per candidate" in system
    assert "advisory only" in system
    assert '"ranked"' in system
    assert "BG" in msgs[1]["content"]


def test_policy_knobs_default_to_advisory():
    # Nothing changes for an objective that has never heard of triage: it runs,
    # it ranks, and every candidate still reaches the judges.
    assert triage_enabled(None) is True
    assert deep_judge_top_n(None) == 0
    assert triage_model(None) == DEFAULT_TRIAGE_MODEL
    assert triage_enabled({"triage": {"enabled": False}}) is False
    assert triage_enabled({"triage": {"enabled": "no"}}) is False
    assert deep_judge_top_n({"triage": {"deep_judge_top_n": 3}}) == 3
    assert deep_judge_top_n({"triage": {"deep_judge_top_n": "junk"}}) == 0
    assert triage_model({"triage": {"model": "deepseek-chat"}}) == "deepseek-chat"


def test_parse_refuses_a_symbol_the_run_never_proposed():
    # A hallucinated ticker must not reach the judges as though the loop picked
    # it, or the audit trail would say the loop did.
    raw = '{"ranked":[{"symbol":"NVDA","worth":0.9,"why":"a"},{"symbol":"FAKE","worth":1,"why":"b"}]}'
    ranked = _parse_ranked(raw, {"NVDA", "AAPL"})
    assert [r["symbol"] for r in ranked] == ["NVDA", "AAPL"]
    # AAPL was proposed and the model dropped it; it keeps its place at the back
    # rather than disappearing from the record.
    assert ranked[1]["why"] == "not ranked by the triage model"


def test_parse_clamps_worth_and_tolerates_fenced_json():
    ranked = _parse_ranked('```json\n{"ranked":[{"symbol":"A","worth":5,"why":"x"}]}\n```', {"A"})
    assert ranked == [{"symbol": "A", "worth": 1.0, "why": "x"}]
    assert _parse_ranked('{"ranked":[{"symbol":"A","worth":-9,"why":""}]}', {"A"})[0]["worth"] == 0.0
    assert _parse_ranked("not json", {"A"}) is None
    assert _parse_ranked('{"ranked":[]}', {"A"}) is None


def test_heuristic_ranking_falls_back_to_the_score_selection_already_computed():
    items = [{"symbol": "a", "score": 10}, {"symbol": "b", "score": 40}, {"symbol": "c"}]
    ranked = heuristic_ranked(items)
    assert [r["symbol"] for r in ranked] == ["B", "A", "C"]
    assert ranked[0]["worth"] == 1.0
    assert ranked[1]["worth"] == 0.25
    assert "no model ranking" in ranked[0]["why"]


def test_top_n_zero_judges_everything():
    items = [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]
    ranked = [{"symbol": s, "worth": 1.0, "why": ""} for s in ("C", "A", "B")]
    deep, held = split_for_deep(items, ranked, 0)
    assert deep == items and held == []
    # A cap at or above the batch size is also "judge everything".
    assert split_for_deep(items, ranked, 9)[1] == []


def test_top_n_narrows_to_the_ranked_head_and_names_what_it_held():
    items = [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]
    ranked = [{"symbol": s, "worth": w, "why": ""} for s, w in (("C", 0.9), ("A", 0.5), ("B", 0.1))]
    deep, held = split_for_deep(items, ranked, 2)
    assert [i["symbol"] for i in deep] == ["C", "A"]
    # Held candidates are named, in ranked order, so the batch can account for
    # every symbol the run proposed.
    assert held == ["B"]


def test_decision_line_says_advisory_until_a_cap_actually_holds_something():
    advisory = {"status": "ok", "source": "llm", "ranked": [1, 2, 3], "cost_usd": 0.0017}
    assert triage_decision(advisory, [1, 2, 3], []) == "source=llm ranked=3 deep=3 (advisory) $0.0017"
    narrowed = {"status": "ok", "source": "llm", "ranked": [1, 2, 3], "cost_usd": 0.0017}
    assert "deep=1 held=2" in triage_decision(narrowed, [1], ["B", "C"])
    assert triage_decision({"status": "skipped"}, [], []) == "no candidates to triage"
    assert "error=boom" in triage_decision({"status": "ok", "source": "heuristic", "ranked": [], "error": "boom"}, [], [])
