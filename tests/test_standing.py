"""The autopilot as standing — assembled from what already exists."""

from __future__ import annotations

from datetime import UTC, datetime

from bifrost_research.copilot.harness import standing
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
    # A policy with no filters used to fall back to the description, so the
    # Autopilot page repeated a claim the policy did not implement — one
    # objective read "IV ≥ 90 and VRP hot" while holding no filter at all.
    # The line now says what the run does; the description is not evidence.
    assert hunts_line({"description": "  free text  "}) == "option scan snapshot · ranked, not screened"
    assert (
        hunts_line({"description": "IV ≥ 90 and VRP hot", "policy_json": {"max_candidates": 3}})
        == "option scan snapshot · ranked, not screened · up to 3 a run"
    )


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
    assert tr["days"] == 730

    monkeypatch.setattr(co, "build_summary", lambda conn, *, days, **kw: {"horizons": [], "pending": 0})
    assert mod.track_record(None, "obj-x")["status"] == "none_settled"


# ── the queue counted the way the Inbox counts it ──────────────────────────


class _DraftRepo:
    """Stands in for `repositories.ai_draft` with a fixed pending list."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def list_drafts(self, _conn, **_kw):
        return self.rows


def _batch(oid: str, symbols: list[str]) -> dict:
    return {"payload": {"objective_id": oid, "items": [{"symbol": s} for s in symbols]}}


def _install(monkeypatch, rows: list[dict]) -> None:
    from bifrost_research.repositories import ai_draft

    monkeypatch.setattr(ai_draft, "list_drafts", _DraftRepo(rows).list_drafts)


def test_repeats_of_the_same_names_are_one_call(monkeypatch) -> None:
    # Eleven re-runs of one objective proposing the same eight names is one
    # decision. The Inbox folded them; this side reported eleven memos waiting.
    rows = [_batch("obj-a", ["HALO", "LPG", "EE"]) for _ in range(11)]
    _install(monkeypatch, rows)
    out = standing.pending_memos_by_objective(object())
    assert out["obj-a"] == {"calls": 1, "drafts": 11}


def test_symbol_order_and_case_do_not_split_a_call(monkeypatch) -> None:
    _install(monkeypatch, [_batch("obj-a", ["halo", "ee"]), _batch("obj-a", ["EE", "HALO"])])
    assert standing.pending_memos_by_objective(object())["obj-a"] == {"calls": 1, "drafts": 2}


def test_different_names_and_different_objectives_stay_separate(monkeypatch) -> None:
    _install(
        monkeypatch,
        [
            _batch("obj-a", ["HALO"]),
            _batch("obj-a", ["NVDA"]),
            _batch("obj-b", ["HALO"]),
        ],
    )
    out = standing.pending_memos_by_objective(object())
    assert out["obj-a"] == {"calls": 2, "drafts": 2}
    assert out["obj-b"] == {"calls": 1, "drafts": 1}


def test_a_batch_with_no_symbols_stands_alone(monkeypatch) -> None:
    # Nothing to match on: folding two empty batches would hide a real card.
    _install(monkeypatch, [_batch("obj-a", []), _batch("obj-a", [])])
    assert standing.pending_memos_by_objective(object())["obj-a"] == {"calls": 2, "drafts": 2}


def _no_outcomes(monkeypatch) -> None:
    import bifrost_research.api.candidate_outcome as co

    monkeypatch.setattr(co, "build_summary", lambda conn, *, days, **kw: {"horizons": [], "pending": 0})


def test_objective_standing_reports_calls_and_the_rows_behind_them(monkeypatch) -> None:
    _no_outcomes(monkeypatch)
    row = standing.objective_standing(None, {"id": "obj-a", "title": "A"}, [], {"calls": 3, "drafts": 21})
    assert row["pending_memos"] == 3
    assert row["pending_drafts"] == 21


def test_objective_standing_still_accepts_a_bare_count(monkeypatch) -> None:
    _no_outcomes(monkeypatch)
    row = standing.objective_standing(None, {"id": "obj-a", "title": "A"}, [], 4)
    assert row["pending_memos"] == 4 and row["pending_drafts"] == 4


# ── the hunts line says what the policy does ───────────────────────────────


def test_a_policy_with_no_filters_says_it_ranks_rather_than_screens():
    # The objective that claimed "iv_rank >= 90" while holding no filter at all.
    line = hunts_line(
        {
            "policy_json": {"source": "harness", "seed_symbols": ["AAPL"], "max_candidates": 3},
            "description": "Every open — pick 3 candidates with iv_rank>=90 and vrp:hot.",
        }
    )
    assert line == "option scan snapshot · ranked, not screened · up to 3 a run"
    assert "iv_rank" not in line, "the description must not stand in for the policy"


def test_scan_filters_are_named_when_they_exist():
    line = hunts_line(
        {
            "policy_json": {
                "universe_mode": "scan_legacy",
                "flag_filter": "iv_rank:hot",
                "min_composite_score": 70,
                "preset": "momentum",
                "max_candidates": 5,
            }
        }
    )
    assert line == "iv_rank:hot · momentum preset · composite ≥ 70 · up to 5 a run"


def test_the_description_fallback_cuts_on_a_word():
    # Reachable only when even the stock funnel says nothing: no stages, no
    # score floor, no overlay. Then the description is all there is.
    long = "A run that " + "explains itself at length " * 8
    line = hunts_line(
        {
            "policy_json": {"universe_mode": "stock_composite", "layers": {"sepa": {"stage": []}}},
            "description": long,
        }
    )
    assert line.endswith("…")
    assert "…" in line and not line.replace("…", "").endswith(" ")
    # No mid-word cut: everything before the ellipsis is whole words.
    assert all(w in long for w in line.split("…")[0].split())


def test_the_purse_reads_the_caps_the_judge_obeys(monkeypatch) -> None:
    # A hardcoded pair here said $1.50/$2.00 while the manifests were rebalanced
    # to $2.25/$1.25; the page would have drawn a ceiling the run does not obey.
    monkeypatch.setenv("PERSONA_EVAL_DAILY_CAP_USD_DEEPSEEK", "2.25")
    monkeypatch.setenv("PERSONA_EVAL_DAILY_CAP_USD_OPENAI", "1.25")
    from bifrost_research.repositories import ai_action_log

    monkeypatch.setattr(
        ai_action_log, "spend_today_by_provider", lambda conn, *, action_kind: {"deepseek": 2.30}
    )
    purse = standing.purse_today(object())
    caps = {p["provider"]: p["cap_usd"] for p in purse["providers"]}
    assert caps == {"deepseek": 2.25, "openai": 1.25}
    assert purse["cap_usd"] == 3.5
    # Spent past its own ceiling: that provider's judge will fall back today.
    assert [p["exhausted"] for p in purse["providers"]] == [True, False]
