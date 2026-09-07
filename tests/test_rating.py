"""Bifrost Rating — a pure function of the candidate's own record."""

from __future__ import annotations

from bifrost_research.copilot.harness.rating import (
    action_for,
    conviction_for,
    instrument_for,
    levels_for,
    most_severe_stance,
    outlook_for,
    rate_candidate,
    rate_items,
    rating_decision,
    settled_record,
    timing_for,
)


def item(**over):
    """NVDA from run_1a07c0fe66e48109e, then overrides."""
    base = {
        "symbol": "NVDA",
        "net_stance": "dissent",
        "agreement": "dissent",
        "blocked_by_validate": False,
        "evidence": {
            "selection": {"path": "PIVOT", "grade": "A", "stage": "STAGE_2A", "sepa_score": 81.6879},
            "price_context": {"close": 228.45, "sma_50": 209.874, "sma_200": 196.46, "high_52w": 236.54, "low_52w": 164.07},
            "option_analytics": {"status": "ok", "iv_rank_1y": 19.0},
            "track_record": {"horizons": [{"horizon_days": 1, "judged": 8, "hit_rate": 0.5}]},
            "agent_verdicts": [
                {"agent": "analyze", "stance": "caution", "model": "deepseek-chat"},
                {"agent": "portfolio", "stance": "oppose", "model": "deepseek-chat"},
                {"agent": "validate", "stance": "caution", "model": "deepseek-chat"},
                {"agent": "verdict", "stance": "caution", "model": "deepseek-chat"},
                {"agent": "analyze", "stance": "support", "model": "gpt-4o-mini"},
                {"agent": "portfolio", "stance": "support", "model": "gpt-4o-mini"},
                {"agent": "validate", "stance": "abstain", "model": "gpt-4o-mini"},
                {"agent": "verdict", "stance": "support", "model": "gpt-4o-mini"},
            ],
        },
    }
    base.update(over)
    return base


def test_nvda_reads_as_the_proposal_said_it_would():
    r = rate_candidate(item())
    assert r["grade"] == "A" and r["grade_score"] == 81.69
    assert r["conviction"] == 2 and r["conviction_reason"] == "judges dissent"
    # Portfolio said oppose: a name already held is Hold, not Watch.
    assert r["action"] == "hold_no_add"
    assert r["levels"] == {
        "pivot": 236.54,
        "entry_lo": 236.54,
        "entry_hi": 248.37,
        "stop": 217.62,
        "stop_source": "8% cap",
        "risk_pct": 8.0,
        "target_2r": 274.39,
        "target_3r": 293.31,
        "rr": 2.0,
    }
    assert r["timing"]["zone"] == "below_pivot"
    assert r["timing"]["pct_vs_pivot"] == -3.42
    assert r["instrument"]["suggestion"] == "Buy stock · long calls"
    assert "judges dissent" in r["why"] and "IV rank 19" in r["why"]
    # The case travels with the grade: the memo shows it without the draft.
    assert r["basis"]["path"] == "PIVOT" and r["basis"]["sma_200"] == 196.46
    assert r["basis"]["invalidation"] == []


def test_the_harsher_judge_sets_the_persona_stance():
    # The leash reads the harsher validate; the rating must not read the
    # candidate more kindly than the gate that decides whether it is accepted.
    v = item()["evidence"]["agent_verdicts"]
    assert most_severe_stance(v, "validate") == "caution"
    assert most_severe_stance(v, "portfolio") == "oppose"
    assert most_severe_stance([], "validate") == "abstain"


def test_stars_apply_the_rules_in_order_with_evidence_first():
    k = {"agreement": "agree", "net": "support", "validate": "support", "portfolio": "support", "blocked": False, "hit_rate": 0.6, "judged": 8}
    assert conviction_for(**k) == (5, "judges agree, validated, record ≥ 0.5, book has room")
    assert conviction_for(**{**k, "portfolio": "oppose"})[0] == 4
    assert conviction_for(**{**k, "net": "caution"})[0] == 4
    assert conviction_for(**{**k, "validate": "caution"})[0] == 3
    assert conviction_for(**{**k, "hit_rate": 0.3}) == (3, "validated but record 0.30 below 0.5")
    # Thin evidence caps at two stars whatever the judges said.
    assert conviction_for(**{**k, "judged": 4}) == (2, "settled record too thin (4 of 5)")
    assert conviction_for(**{**k, "validate": "abstain"}) == (2, "no settled record to validate")
    assert conviction_for(**{**k, "agreement": "dissent"}) == (2, "judges dissent")
    assert conviction_for(**{**k, "blocked": True}) == (1, "validate opposed")
    assert conviction_for(**{**k, "validate": "oppose"})[0] == 1
    # A single judge can carry a setup to three stars and no further.
    assert conviction_for(**{**k, "agreement": None}) == (3, "single judge")


def test_action_table():
    base = {"grade": "A", "stage": "STAGE_2A", "conviction": 3, "zone": "in_zone", "above_50d": True, "portfolio": "support", "blocked": False, "validate": "caution"}
    assert action_for(**base) == ("buy_zone", "at the pivot with backing")
    assert action_for(**{**base, "zone": "extended"})[0] == "extended"
    assert action_for(**{**base, "zone": "below_pivot"})[0] == "accumulate"
    assert action_for(**{**base, "zone": "below_pivot", "above_50d": False})[0] == "watch"
    assert action_for(**{**base, "conviction": 2})[0] == "watch"
    assert action_for(**{**base, "portfolio": "oppose"})[0] == "hold_no_add"
    assert action_for(**{**base, "validate": "oppose"})[0] == "avoid"
    assert action_for(**{**base, "grade": "C"})[0] == "avoid"
    assert action_for(**{**base, "stage": "STAGE_4"})[0] == "avoid"


def test_levels_use_the_tighter_of_the_50d_and_the_8pct_cap():
    # 50d above the cap: it is the tighter stop.
    lv = levels_for(100.0, 95.0)
    assert lv["stop"] == 95.0 and lv["stop_source"] == "50d"
    assert lv["entry_hi"] == 105.0 and lv["target_2r"] == 110.0 and lv["risk_pct"] == 5.0
    # 50d below the cap: the cap wins, never wider than 8 %.
    lv = levels_for(100.0, 80.0)
    assert lv["stop"] == 92.0 and lv["stop_source"] == "8% cap"
    # A 50d above the pivot cannot be a stop; the cap is used.
    assert levels_for(100.0, 101.0)["stop_source"] == "8% cap"
    assert levels_for(None, 95.0) is None
    assert levels_for(0, 95.0) is None


def test_timing_zones():
    assert timing_for(100.0, 100.0, 90.0)["zone"] == "in_zone"
    assert timing_for(97.5, 100.0, 90.0)["zone"] == "in_zone"
    assert timing_for(96.0, 100.0, 90.0)["zone"] == "below_pivot"
    assert timing_for(106.0, 100.0, 90.0)["zone"] == "extended"
    assert timing_for(None, 100.0, 90.0)["zone"] == "unknown"
    t = timing_for(100.0, None, 90.0)
    assert t["zone"] == "unknown" and t["above_50d"] is True and t["pct_vs_50d"] == 11.11


def test_outlook_needs_a_prior_on_the_same_ruler():
    assert outlook_for(77.0, 82.0) == ("softening", {"from": 82.0, "to": 77.0, "delta": -5.0})
    assert outlook_for(82.0, 77.0)[0] == "improving"
    assert outlook_for(79.0, 78.0)[0] == "stable"
    assert outlook_for(79.0, None) == (None, None)


def test_instrument_grid():
    assert instrument_for("STAGE_2A", 19.0)["suggestion"] == "Buy stock · long calls"
    assert instrument_for("STAGE_2B", 75.0)["suggestion"].startswith("Buy stock + covered calls")
    assert instrument_for("STAGE_3", 70.0)["suggestion"] == "Sell premium (range)"
    assert instrument_for("STAGE_4", 50.0)["suggestion"] == "Avoid"
    # Unmeasured IV is said, not guessed.
    im = instrument_for("STAGE_2A", None)
    assert im["suggestion"] is None and "not measured" in im["note"]


def test_settled_record_takes_the_longest_judged_horizon():
    track = {"horizons": [{"horizon_days": 1, "judged": 8, "hit_rate": 0.5}, {"horizon_days": 20, "judged": 3, "hit_rate": 1.0}, {"horizon_days": 5, "judged": 0}]}
    assert settled_record(track) == (1.0, 3)
    assert settled_record({}) == (None, 0)


def test_rate_items_attaches_and_orders_best_first():
    items = [
        item(symbol="FAR", evidence={**item()["evidence"], "price_context": {"close": 90.0, "sma_50": 95.0, "high_52w": 100.0}}),
        item(symbol="NEAR", evidence={**item()["evidence"], "price_context": {"close": 99.5, "sma_50": 95.0, "high_52w": 100.0}}),
        item(symbol="BLOCKED", blocked_by_validate=True),
    ]
    # Strip the portfolio-oppose row so FAR / NEAR are Watch, not Hold.
    for it in items[:2]:
        it["evidence"]["agent_verdicts"] = [v for v in it["evidence"]["agent_verdicts"] if v["agent"] != "portfolio"]
    ratings = rate_items(items)
    # At the pivot before below it; blocked last. Every item now carries its rating.
    assert [r["symbol"] for r in ratings] == ["NEAR", "FAR", "BLOCKED"]
    assert all("rating" in it for it in items)
    assert ratings[-1]["levels"] is None
    assert rating_decision(ratings) == "best ★2 · 2 watch · 1 avoid"
    assert rating_decision([]) == "nothing to rate"


def test_a_candidate_with_no_evidence_still_rates_without_raising():
    r = rate_candidate({"symbol": "ee"})
    assert r["symbol"] == "EE" and r["grade"] is None and r["levels"] is None
    assert r["conviction"] == 2 and r["action"] == "watch"
    assert r["timing"]["zone"] == "unknown"
