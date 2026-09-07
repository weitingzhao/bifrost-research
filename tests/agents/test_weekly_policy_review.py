"""research-loop-automation D3 — the rules get a weekly proposal from settled outcomes."""

from __future__ import annotations

from datetime import date
from typing import Any

from bifrost_research.copilot.agents import weekly_policy_review as W

OBJ = {"id": "obj_stock", "title": "Daily Loop Stock Explorer", "status": "active", "policy_json": {"universe_mode": "stock_composite", "max_candidates": 8, "layers": {"sepa": {"min_score": 70}}}}
WEAK = {"objective_id": "obj_stock", "days": 90, "candidates": 30, "horizons": [{"horizon_days": 5, "settled": 24, "judged": 24, "hits": 8, "hit_rate": 8 / 24}], "pending": 6}
FINE = {"objective_id": "obj_stock", "days": 90, "candidates": 30, "horizons": [{"horizon_days": 5, "settled": 24, "judged": 24, "hits": 14, "hit_rate": 14 / 24}], "pending": 6}


def _wire(monkeypatch: Any, *, summary: dict[str, Any], existing: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    w: dict[str, Any] = {"drafts": [], "actions": [], "summary_calls": []}
    monkeypatch.setattr(W.obj_repo, "list_objectives", lambda conn, **kw: [OBJ])
    monkeypatch.setattr(W.obj_repo, "list_runs", lambda conn, **kw: [{"id": "run_1", "outputs": {"persona_eval": {"symbols_evaluated": 8, "blocked_by_validate": 1}}}])
    monkeypatch.setattr(W.draft_repo, "list_drafts", lambda conn, **kw: existing or [])
    monkeypatch.setattr(W.draft_repo, "insert_draft", lambda conn, **kw: w["drafts"].append(kw) or {"id": f"draft_{len(w['drafts'])}", **kw})
    monkeypatch.setattr(W.action_repo, "insert_action", lambda conn, **kw: w["actions"].append(kw) or {"id": "act_1", **kw})

    def _summary(conn, *, objective_id=None, days=90, **kw):
        w["summary_calls"].append((objective_id, days))
        return summary

    monkeypatch.setattr("bifrost_research.api.candidate_outcome.build_summary", _summary)
    return w


def test_week_label_is_iso() -> None:
    assert W.week_label(date(2026, 9, 6)) == "2026-W36"  # a Sunday belongs to the week that ends on it
    assert W.week_label(date(2026, 9, 7)) == "2026-W37"


def test_a_weak_record_becomes_a_policy_suggestion_with_its_evidence(monkeypatch) -> None:
    w = _wire(monkeypatch, summary=WEAK)
    out = W.run_weekly_policy_review(object(), day=date(2026, 9, 6))
    assert out["ok"] is True and out["week"] == "2026-W36" and out["objectives"] == 1
    assert w["summary_calls"] == [("obj_stock", 90)]  # judged on its own candidates, not the shared pool
    assert out["proposed"] == ["draft_1"]
    draft = w["drafts"][0]
    assert draft["kind"] == "policy_suggestion" and draft["scope"] == "objective:obj_stock" and draft["generated_by"] == "weekly_policy_review"
    payload = draft["payload"]
    assert payload["source"] == "weekly_outcomes" and payload["week"] == "2026-W36"
    assert payload["suggestion"]["layers"]["sepa"]["min_score"] == 78.0  # weak outcomes → the SEPA floor rises by 8
    assert payload["suggestion"]["max_candidates"] == 7 and payload["suggestion"]["require_validate_pass"] is True
    assert payload["evidence"]["outcome_summary"] is WEAK and payload["evidence"]["judged_outcomes"] == 24
    assert "hit_rate=33%" in payload["llm_reasoning"]
    assert w["actions"][0]["action_kind"] == "weekly_policy_review" and w["actions"][0]["status"] == "proposed"


def test_a_fine_record_leaves_a_ledger_row_and_no_draft(monkeypatch) -> None:
    monkeypatch.setattr(W.obj_repo, "list_runs", lambda conn, **kw: [])  # no persona dissent either
    w = _wire(monkeypatch, summary=FINE)
    monkeypatch.setattr(W.obj_repo, "list_runs", lambda conn, **kw: [])
    out = W.run_weekly_policy_review(object(), day=date(2026, 9, 6))
    assert out["proposed"] == [] and out["reviewed"] == [{"objective_id": "obj_stock", "proposed": False, "judged_outcomes": 24}]
    assert w["drafts"] == []
    assert w["actions"][0]["status"] == "executed" and w["actions"][0]["output_payload"]["proposed"] is False


def test_one_proposal_per_objective_per_week_unless_forced(monkeypatch) -> None:
    existing = [{"id": "draft_old", "kind": "policy_suggestion", "payload": {"source": "weekly_outcomes", "week": "2026-W36"}}]
    w = _wire(monkeypatch, summary=WEAK, existing=existing)
    out = W.run_weekly_policy_review(object(), day=date(2026, 9, 6))
    assert out["reviewed"][0] == {"objective_id": "obj_stock", "skipped": True, "reason": "already proposed this week", "draft_id": "draft_old"}
    assert w["drafts"] == [] and w["summary_calls"] == []
    forced = W.run_weekly_policy_review(object(), day=date(2026, 9, 6), force=True)
    assert forced["proposed"] == ["draft_1"]


def test_one_objective_failing_does_not_stop_the_review(monkeypatch) -> None:
    w = _wire(monkeypatch, summary=WEAK)
    monkeypatch.setattr(W.obj_repo, "list_objectives", lambda conn, **kw: [OBJ, {"id": "obj_broken", "policy_json": {}}])

    def _summary(conn, *, objective_id=None, days=90, **kw):
        if objective_id == "obj_broken":
            raise RuntimeError("ledger unreadable")
        return WEAK

    monkeypatch.setattr("bifrost_research.api.candidate_outcome.build_summary", _summary)

    class _Conn:
        def rollback(self) -> None:
            pass

    out = W.run_weekly_policy_review(_Conn(), day=date(2026, 9, 6))
    assert out["proposed"] == ["draft_1"]
    assert out["reviewed"][1] == {"objective_id": "obj_broken", "error": "ledger unreadable"}
    assert len(w["drafts"]) == 1
