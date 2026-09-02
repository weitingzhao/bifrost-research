"""The planner's two new hands, and the rules that guide them — P1.

An op in the whitelist with no branch in the runtime is a plan that describes a
step which never runs; plan_llm says so in its own comment. These tests hold both
halves together, and hold the report to the one claim that is easy to fake: how
this source has actually settled.
"""

from __future__ import annotations

import inspect

from bifrost_research.copilot.harness import runtime as rt
from bifrost_research.copilot.harness.plan_llm import (
    OP_COMPOSE_REPORT,
    OP_RUN_BACKTEST,
    VALID_OPS,
    _playbook_block,
)
from bifrost_research.copilot.harness.report import compose_report

OBJ = {"id": "obj-1", "title": "Daily Loop"}


# ---------------- whitelist and runtime stay in step ----------------


def test_both_new_ops_are_planner_visible() -> None:
    assert OP_RUN_BACKTEST in VALID_OPS
    assert OP_COMPOSE_REPORT in VALID_OPS


def test_each_new_op_has_a_runtime_branch() -> None:
    """Otherwise the plan names a step that never executes.

    That is the exact failure the analyze_symbol work fixed once already: the
    plan was written into the trace and never read, so an LLM "planner" could
    not change what happened.
    """
    src = inspect.getsource(rt.run_objective)
    assert "want_backtest = OP_RUN_BACKTEST in plan_ops" in src
    assert "want_report = OP_COMPOSE_REPORT in plan_ops" in src
    assert "compose_report(" in src


# ---------------- the report's four sections ----------------


def _item(symbol: str, *, settled: bool) -> dict:
    return {
        "symbol": symbol,
        "score": 82.8,
        "evidence": {
            "selection": {"path": "PIVOT", "grade": "A", "sepa_score": 82.8},
            "price_context": {"close": 100.0, "sma_50": 95.0},
            "track_record": (
                {"horizons": [{"horizon_days": 20, "hit_rate": 0.6}]}
                if settled
                else {"status": "not_measured", "reason": "nothing has settled yet"}
            ),
            "invalidation": ["sepa_score falls below 70"],
        },
    }


def test_every_recommendation_carries_all_four_sections() -> None:
    rep = compose_report(
        objective=OBJ, run_id="run-1", items=[_item("PAYS", settled=True)],
        funnel=[], backtest=None,
    )
    sec = rep["candidates"][0]
    assert sec["why"]["path"] == "PIVOT"
    assert sec["price"]["close"] == 100.0
    assert sec["settled"]["status"] == "measured"
    assert sec["wrong_if"] == ["sepa_score falls below 70"]


def test_an_unsettled_source_says_so_rather_than_reading_as_zero() -> None:
    """not_measured is a fact about coverage, not a verdict on the stock.

    Rendering an absent record as 0% would be a lie the reader cannot see
    through — it would read as "this signal has never worked".
    """
    rep = compose_report(
        objective=OBJ, run_id="run-1", items=[_item("NNE", settled=False)],
        funnel=[], backtest=None,
    )
    settled = rep["candidates"][0]["settled"]
    assert settled["status"] == "not_measured"
    assert "settled" in settled["reason"]
    assert "hit_rate" not in settled


def test_coverage_states_how_much_of_the_report_rests_on_measured_history() -> None:
    rep = compose_report(
        objective=OBJ,
        run_id="run-1",
        items=[_item("PAYS", settled=True), _item("NNE", settled=False)],
        funnel=[],
        backtest=None,
    )
    cov = rep["coverage"]
    assert cov["candidates"] == 2
    assert cov["with_settled_record"] == 1
    assert "1 of 2" in cov["note"]


def test_the_report_carries_the_funnel_and_stays_advisory() -> None:
    rep = compose_report(
        objective=OBJ,
        run_id="run-1",
        items=[_item("PAYS", settled=True)],
        funnel=[{"name": "sepa", "in_count": 3472, "out_count": 47}],
        backtest={"status": "ok", "summary": {"win_rate": 0.55}},
    )
    assert rep["funnel"][0]["in_count"] == 3472
    assert rep["backtest"]["summary"]["win_rate"] == 0.55
    assert "D10 BLOCKED" in rep["advisory"]


def test_an_empty_batch_does_not_claim_coverage() -> None:
    rep = compose_report(
        objective=OBJ, run_id="run-1", items=[], funnel=None, backtest=None
    )
    assert rep["coverage"]["candidates"] == 0
    assert "no candidates" in rep["coverage"]["note"]


# ---------------- owner rules reach the planner ----------------


def test_rules_reach_the_prompt_and_are_marked_as_outranking() -> None:
    block = _playbook_block(
        [{"title": "No earnings week", "body_md": "Skip names reporting within 5 days."}]
    )
    assert "No earnings week" in block
    assert "Skip names reporting within 5 days." in block
    assert "outrank" in block


def test_no_rules_changes_the_prompt_not_at_all() -> None:
    """An empty Playbook must leave the planner exactly as it was."""
    assert _playbook_block([]) == ""
    assert _playbook_block(None) == ""
    assert _playbook_block([{"title": "", "body_md": ""}]) == ""
