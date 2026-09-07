"""The autopilot as standing — assembled from what already exists."""

from __future__ import annotations

from datetime import UTC, datetime

from bifrost_research.copilot.harness.rating import rating_summary
from bifrost_research.copilot.harness.standing import (
    hunts_line,
    last_memo,
    next_scheduled_run,
    spend_30d,
)


def test_next_run_is_the_next_weekday_1330_utc():
    fri_after = datetime(2026, 9, 4, 14, 0, tzinfo=UTC)  # Friday 14:00 → Monday
    assert next_scheduled_run(fri_after) == "2026-09-07T13:30:00+00:00"
    mon_before = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
    assert next_scheduled_run(mon_before) == "2026-09-07T13:30:00+00:00"
    sat = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    assert next_scheduled_run(sat) == "2026-09-07T13:30:00+00:00"


def test_headline_matches_the_drawer_word_for_word():
    r = lambda sym, action, conv, agreement="agree", blocked=False, validate="caution": {
        "symbol": sym, "action": action, "conviction": conv, "grade": "A",
        "inputs": {"agreement": agreement, "blocked": blocked, "validate": validate},
    }
    ratings = [r("NVDA", "hold_no_add", 2, "dissent"), r("BG", "avoid", 1, blocked=True), r("LPG", "watch", 2)]
    s = rating_summary(ratings, considered=3475)
    assert s["headline"] == "3 candidates from 3,475. None above ★★ — nothing actionable yet. judges split on 1, validate blocked 1."
    assert s["best"] == 2 and s["actionable"] == 0 and s["split"] == 1 and s["blocked"] == 1
    buy = rating_summary([r("A", "buy_zone", 4), r("B", "watch", 2, "dissent")])
    assert buy["headline"] == "2 candidates. 1 actionable, best ★★★★. judges split on 1."
    assert rating_summary([])["headline"] == "No candidates were rated."


def test_last_memo_takes_the_newest_rated_run_and_its_funnel():
    unrated = {"id": "run_new", "outputs": {}}
    rated = {
        "id": "run_old",
        "started_at": "2026-09-06T23:57:00+00:00",
        "status": "awaiting_approval",
        "outputs": {"ratings": [{"symbol": "HALO", "action": "watch", "conviction": 2, "grade": "A", "inputs": {"agreement": "dissent"}}]},
        "trace_json": {"events": [{"step": "scan_universe", "funnel": [{"in_count": 3475, "out_count": 42}]}]},
    }
    memo = last_memo([unrated, rated])
    assert memo["run_id"] == "run_old"
    assert memo["headline"].startswith("1 candidate from 3,475.")
    assert memo["picks"] == [{"symbol": "HALO", "action": "watch", "conviction": 2, "grade": "A"}]
    assert last_memo([unrated]) is None


def test_spend_adds_judges_triage_and_planner_across_runs():
    runs = [
        {"outputs": {"persona_eval": {"models": [{"cost_usd": 0.5}, {"cost_usd": 0.08}]}, "triage": {"cost_usd": 0.0002}}, "plan_json": {"llm_attempts": [{"cost_usd": 0.0002}]}},
        {"outputs": {"persona_eval": {"models": [{"cost_usd": "bad"}]}}},
    ]
    assert spend_30d(runs) == 0.5804


def test_hunts_line_reads_the_policy_not_the_prose():
    stock = {"policy_json": {"universe_mode": "stock_composite", "layers": {"sepa": {"stage": ["SETUP", "PIVOT"], "min_score": 70}}, "option_overlay": {"enabled": True, "flag_filter": "iv_rank:hot"}, "max_candidates": 8}}
    assert hunts_line(stock) == "SETUP/PIVOT names · SEPA ≥ 70 · option overlay iv_rank:hot · up to 8 a run"
    scan = {"policy_json": {"universe_mode": "scan_legacy", "flag_filter": ["iv_rank:hot", "vrp:hot"], "max_candidates": 3}}
    assert hunts_line(scan) == "iv_rank:hot, vrp:hot · up to 3 a run"
    assert hunts_line({"description": "  free text  "}) == "free text"
    # No legible filter: the description carries the meaning, the cap follows.
    assert hunts_line({"description": "IV ≥ 90 and VRP hot", "policy_json": {"max_candidates": 3}}) == "IV ≥ 90 and VRP hot · up to 3 a run"


def test_track_record_falls_back_to_the_harness_wide_record_and_says_so(monkeypatch):
    import bifrost_research.api.candidate_outcome as co
    from bifrost_research.copilot.harness import standing as mod

    calls = []

    def fake_summary(conn, *, days, objective_id=None, source=None):
        calls.append((objective_id, source))
        if objective_id:
            return {"horizons": [{"horizon_days": 1, "judged": 0}], "pending": 3}
        return {"horizons": [{"horizon_days": 1, "judged": 8, "hit_rate": 0.5, "avg_excess": 0.0086}], "pending": 0}

    monkeypatch.setattr(co, "build_summary", fake_summary)
    tr = mod.track_record(None, "obj-x")
    assert tr["status"] == "ok" and tr["scope"] == "source" and tr["hit_rate"] == 0.5 and tr["judged"] == 8
    assert calls == [("obj-x", None), (None, "harness")]

    monkeypatch.setattr(co, "build_summary", lambda conn, *, days, **kw: {"horizons": [], "pending": 0})
    assert mod.track_record(None, "obj-x")["status"] == "none_settled"
