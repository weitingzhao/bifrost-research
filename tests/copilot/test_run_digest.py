"""research-loop-automation D1 — a run digested from its own record."""

from __future__ import annotations

from typing import Any

from bifrost_research.copilot.harness import run_digest as RD

RUN = {
    "id": "run_1",
    "objective_id": "obj_stock",
    "status": "awaiting_approval",
    "started_at": "2026-09-06T14:00:00+00:00",
    "finished_at": "2026-09-06T14:09:00+00:00",
    "plan_json": {
        "generated_by": "llm",
        "llm_model": "deepseek-chat",
        "llm_attempts": [{"model": "deepseek-chat", "ok": True}],
        "steps": [{"op": "scan_universe"}, {"op": "analyze_symbol"}, {"op": "persona_evaluate"}],
    },
    "trace_json": {
        "events": [
            {"step": "plan", "label": "Plan", "decision": "generated_by=llm model=deepseek-chat", "at_ms": 0},
            {"step": "persona_evaluate", "label": "Persona eval", "decision": "mode=agent models=2 agree=1 dissent=1", "at_ms": 448000, "per_symbol": {"WT": {}}},
        ],
        "progress": {"step": "awaiting_approval", "detail": "candidates=2 drafts=1"},
    },
    "outputs": {"candidate_ids": ["cand_wt", "cand_lpg"], "draft_ids": ["draft_1"], "data_source": "scan", "universe_mode": "stock_composite", "trust": {"reason": "L1"}},
}

OBJECTIVE = {"id": "obj_stock", "title": "Daily Loop Stock Explorer", "policy_json": {"max_candidates": 8, "persona_evaluate": True}}

WT_VERDICTS = [
    {"agent": "analyze", "stance": "support", "summary": "Stage 2 with room to 52w high", "model": "deepseek-chat", "fallback": False},
    {"agent": "validate", "stance": "caution", "summary": "win rate 0.6 on 5 events", "model": "deepseek-chat", "fallback": False},
    {"agent": "analyze", "stance": "oppose", "summary": "extended", "model": "gpt-4o-mini", "fallback": False},
]

DRAFT = {
    "id": "draft_1",
    "kind": "candidate_batch",
    "status": "pending",
    "payload": {
        "run_id": "run_1",
        "funnel": [{"name": "sepa", "in_count": 3472, "out_count": 47}, {"name": "max_candidates", "in_count": 24, "out_count": 8}],
        "hit_rate_gate": {"applied": True, "ok": True},
        "persona_eval": {"mode": "agent", "models": [{"model": "deepseek-chat"}, {"model": "gpt-4o-mini"}], "agreement": {"agree": 1, "dissent": 1}},
        "items": [
            {
                "id": "cand_wt",
                "symbol": "WT",
                "score": 82.1,
                "net_stance": "dissent",
                "agreement": "dissent",
                "evidence": {
                    "selection": {"status": "ok", "path": "SETUP", "grade": "A", "sepa_score": 82.1},
                    "option_analytics": {"status": "not_measured", "reason": "no option chain history"},
                    "track_record": {"status": "not_measured", "reason": "no candidate from this source has settled"},
                    "invalidation": ["close below the 50-day", "SEPA grade drops below B"],
                    "agent_verdicts": WT_VERDICTS,
                    "secret_token": "should not leak",
                },
            },
            {"id": "cand_lpg", "symbol": "LPG", "score": 71.0, "net_stance": "support", "agreement": "agree", "evidence": {"agent_verdicts": []}},
        ],
        "report": {
            "coverage": {"candidates": 2, "with_settled_record": 0},
            "note": "not measured is a fact about coverage",
            "candidates": [
                {"symbol": "WT", "why": {"path": "SETUP", "grade": "A"}, "price": {"close": 12.3}, "settled": {"status": "not_measured"}, "wrong_if": ["close below the 50-day"], "falsify": ["validate: 5 events only"]},
                {"symbol": "LPG", "why": {"path": "PIVOT"}, "settled": {"status": "not_measured"}, "wrong_if": []},
            ],
        },
    },
}

CANDIDATE_ROW = {"id": "cand_wt", "symbol": "WT", "status": "promoted", "source": "harness", "tags": ["stock"], "hypothesis_id": "hyp_wt", "lens_snapshot": {"sepa": 82.1}, "trade_date": "2026-09-06"}
HYPOTHESIS = {"id": "hyp_wt", "title": "WT SEPA setup", "status": "active", "linked_backtest_ids": ["bt_1"], "resolution_json": None}
BACKTEST = {"id": "bt_1", "strategy_template": "stock_breakout_20d", "summary": {"n_events": 5, "win_rate": 0.6}}


def _wire(monkeypatch: Any, *, run: dict[str, Any] | None = RUN) -> None:
    monkeypatch.setattr(RD.obj_repo, "get_run", lambda conn, run_id: run if run and run_id == run["id"] else None)
    monkeypatch.setattr(RD.obj_repo, "get_objective", lambda conn, oid: OBJECTIVE if oid == "obj_stock" else None)
    monkeypatch.setattr(RD.obj_repo, "list_runs", lambda conn, **kw: [run] if run else [])
    monkeypatch.setattr(RD.draft_repo, "get_draft", lambda conn, did: DRAFT if did == "draft_1" else None)
    monkeypatch.setattr(RD.cand_repo, "get_candidate", lambda conn, cid: CANDIDATE_ROW if cid == "cand_wt" else None)
    monkeypatch.setattr(RD.hyp_repo, "get_hypothesis", lambda conn, hid: HYPOTHESIS if hid == "hyp_wt" else None)
    monkeypatch.setattr(RD.bt_repo, "get_run", lambda conn, bid: BACKTEST if bid == "bt_1" else None)


def test_digest_quotes_plan_funnel_judges_and_report(monkeypatch) -> None:
    _wire(monkeypatch)
    d = RD.digest_run(object(), "run_1")
    assert d is not None
    assert d["run"]["objective_title"] == "Daily Loop Stock Explorer" and d["run"]["status"] == "awaiting_approval"
    assert d["plan"]["generated_by"] == "llm" and d["plan"]["llm_model"] == "deepseek-chat"
    assert d["plan"]["steps"] == ["scan_universe", "analyze_symbol", "persona_evaluate"]
    assert [s["step"] for s in d["steps"]] == ["plan", "persona_evaluate"]
    assert "per_symbol" not in d["steps"][1]  # the compact step list keeps decisions, not payloads
    assert d["funnel"][0] == {"name": "sepa", "in_count": 3472, "out_count": 47}
    assert d["gate"] == {"applied": True, "ok": True}
    assert d["persona"]["agreement"] == {"agree": 1, "dissent": 1}
    wt = d["candidates"][0]
    assert wt["symbol"] == "WT" and wt["net_stance"] == "dissent" and wt["agreement"] == "dissent"
    assert [(v["model"], v["agent"], v["stance"]) for v in wt["verdicts"]] == [
        ("deepseek-chat", "analyze", "support"),
        ("deepseek-chat", "validate", "caution"),
        ("gpt-4o-mini", "analyze", "oppose"),
    ]
    assert wt["evidence"]["track_record"]["status"] == "not_measured"
    assert "secret_token" not in wt["evidence"]
    assert wt["report"]["wrong_if"] == ["close below the 50-day"]
    assert d["report"]["coverage"]["with_settled_record"] == 0
    assert d["outputs"]["trust"] == {"reason": "L1"} and d["drafts"] == [{"id": "draft_1", "kind": "candidate_batch", "status": "pending"}]
    assert d["advisory"].startswith("D10 BLOCKED")


def test_explain_candidate_answers_why_and_what_would_unmake_it(monkeypatch) -> None:
    _wire(monkeypatch)
    e = RD.explain_candidate(object(), "run_1", "wt")
    assert e is not None and e["found"] is True and e["symbol"] == "WT"
    assert e["what_would_unmake_it"] == ["close below the 50-day"]
    assert e["falsify"] == ["validate: 5 events only"]
    assert e["settled"] == {"status": "not_measured"}
    assert e["candidate"]["status"] == "promoted" and e["candidate"]["hypothesis_id"] == "hyp_wt"
    assert e["hypothesis"]["title"] == "WT SEPA setup"
    assert e["validation"] == [{"backtest_run_id": "bt_1", "strategy_template": "stock_breakout_20d", "summary": {"n_events": 5, "win_rate": 0.6}}]
    assert e["evidence"]["selection"]["path"] == "SETUP"

    # a name the run never proposed: say so, and list what it did propose
    missing = RD.explain_candidate(object(), "run_1", "NVDA")
    assert missing is not None and missing["found"] is False and missing["candidates"] == ["WT", "LPG"]
    # invalidation falls back to the evidence when the report has no wrong_if
    lpg = RD.explain_candidate(object(), "run_1", "LPG")
    assert lpg is not None and lpg["what_would_unmake_it"] == [] and lpg["candidate"] is None


def test_unknown_run_is_none_and_list_is_one_line_per_run(monkeypatch) -> None:
    _wire(monkeypatch)
    assert RD.digest_run(object(), "run_404") is None
    assert RD.explain_candidate(object(), "run_404", "WT") is None
    listing = RD.list_runs_digest(object(), limit=5)
    assert listing["count"] == 1
    row = listing["items"][0]
    assert row["objective_title"] == "Daily Loop Stock Explorer"
    assert row["candidates"] == 2 and row["drafts"] == 1
    assert row["plan_generated_by"] == "llm" and row["plan_model"] == "deepseek-chat"
    assert row["progress"] == "candidates=2 drafts=1"


def test_digest_survives_a_run_without_drafts_or_plan(monkeypatch) -> None:
    bare = {"id": "run_1", "objective_id": "obj_stock", "status": "failed", "plan_json": {}, "trace_json": {}, "outputs": {}}
    _wire(monkeypatch, run=bare)
    d = RD.digest_run(object(), "run_1")
    assert d is not None
    assert d["candidates"] == [] and d["report"] is None and d["funnel"] == [] and d["steps"] == []
    assert d["plan"] == {"steps": []}
