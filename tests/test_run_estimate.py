"""Telling the Owner the price before they press the button."""

from __future__ import annotations

import json

from bifrost_research.copilot.harness.run_estimate import (
    estimate_run,
    estimate_summary,
    observed_rates,
    observed_triage_usd,
)


def run(judged: int, models: list[tuple[str, float]], triage_usd: float | None = None) -> dict:
    out: dict = {
        "persona_eval": {
            "symbols_evaluated": judged,
            "models": [{"model": m, "cost_usd": c} for m, c in models],
        }
    }
    if triage_usd is not None:
        out["triage"] = {"cost_usd": triage_usd}
    return out


# The two real runs measured on DEV, 2026-09-07.
RUN_8 = run(8, [("deepseek-chat", 0.5552), ("gpt-4o-mini", 0.0760)])
RUN_3 = run(3, [("deepseek-chat", 0.200157), ("gpt-4o-mini", 0.031321)], triage_usd=0.00014)


def test_rate_per_candidate_is_stable_across_batch_sizes():
    rates = observed_rates([RUN_8, RUN_3])
    # Pooled across 11 candidates: the two batch sizes agree to the third
    # decimal, which is why a per-candidate rate projects at all.
    assert rates["deepseek-chat"]["usd_per_candidate"] == round(0.755357 / 11, 6)
    assert rates["deepseek-chat"]["runs"] == 2
    assert rates["deepseek-chat"]["candidates"] == 11
    assert 0.009 < rates["gpt-4o-mini"]["usd_per_candidate"] < 0.011


def test_a_run_that_judged_nothing_is_skipped_not_counted_as_free():
    # Averaging a failed run's zero in would understate the next run by exactly
    # the fraction of runs that failed.
    rates = observed_rates([RUN_8, run(0, [("deepseek-chat", 0.0)]), {"persona_eval": {}}, None])
    assert rates["deepseek-chat"]["runs"] == 1
    assert rates["deepseek-chat"]["candidates"] == 8


def test_outputs_are_read_whether_stored_as_json_or_as_a_dict():
    assert observed_rates([json.dumps(RUN_3)])["deepseek-chat"]["runs"] == 1
    assert observed_rates(["not json", 42, None]) == {}


def test_estimate_uses_this_objective_history_and_says_so():
    est = estimate_run(runs=[RUN_8, RUN_3], models=["deepseek-chat", "gpt-4o-mini"], candidates=5)
    assert est["source"] == "measured"
    assert est["runs_sampled"] == 2
    # Five candidates at the pooled rates, plus one triage call.
    assert 0.38 < est["total_usd"] < 0.42
    assert [m["source"] for m in est["models"]] == ["measured", "measured"]
    assert est["triage_usd"] == 0.00014
    assert "from this objective's last 2 run(s)" in estimate_summary(est)


def test_one_borrowed_rate_makes_the_whole_figure_an_approximation():
    # Calling a part-borrowed estimate "measured" would overstate what is known,
    # and the reader is about to spend real money on the strength of it.
    est = estimate_run(runs=[RUN_8], models=["deepseek-chat", "claude-3-haiku"], candidates=4)
    assert est["source"] == "typical"
    assert [m["source"] for m in est["models"]] == ["measured", "typical"]
    assert "typical rates" in estimate_summary(est)


def test_an_objective_that_never_judged_still_gets_a_usable_number():
    est = estimate_run(runs=[], models=["deepseek-chat", "gpt-4o-mini"], candidates=8)
    assert est["source"] == "typical"
    assert est["runs_sampled"] == 0
    # 8 × ($0.068 + $0.010) plus the fallback triage call.
    assert 0.62 < est["total_usd"] < 0.63
    assert est["triage_usd"] > 0


def test_dropping_a_model_or_a_candidate_moves_the_figure_the_way_it_should():
    both = estimate_run(runs=[RUN_8, RUN_3], models=["deepseek-chat", "gpt-4o-mini"], candidates=8)
    cheap = estimate_run(runs=[RUN_8, RUN_3], models=["gpt-4o-mini"], candidates=8)
    fewer = estimate_run(runs=[RUN_8, RUN_3], models=["deepseek-chat", "gpt-4o-mini"], candidates=3)
    assert cheap["total_usd"] < both["total_usd"] / 5
    assert fewer["total_usd"] < both["total_usd"]
    assert estimate_run(runs=[], models=[], candidates=8)["total_usd"] > 0  # triage alone


def test_triage_can_be_left_out_of_the_projection():
    with_t = estimate_run(runs=[RUN_3], models=["gpt-4o-mini"], candidates=2)
    without = estimate_run(runs=[RUN_3], models=["gpt-4o-mini"], candidates=2, triage=False)
    assert with_t["total_usd"] > without["total_usd"]
    assert without["triage_usd"] == 0.0


def test_triage_mean_is_none_until_one_has_been_recorded():
    assert observed_triage_usd([RUN_8]) is None
    assert observed_triage_usd([RUN_3, RUN_3]) == 0.00014
