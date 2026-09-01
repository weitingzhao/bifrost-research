"""Per-candidate evidence, and the plan finally controlling something.

Two claims are pinned here. First, that a section with no data says so instead
of rendering blank — a missing option-analytics panel reads as "nothing notable
about this stock" when the truth is "we have no option data for it". Second,
that `analyze_symbol` in the plan is what turns evidence on: before W4 the plan
was written into the trace and never read, so adding ops to the whitelist would
have bought nothing but a more detailed fiction.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.copilot.harness import evidence as ev
from bifrost_research.copilot.harness.plan_llm import OP_ANALYZE_SYMBOL, VALID_OPS

_SEPA_ROW = (
    82.75,  # sepa_score
    "A",  # grade
    "STAGE_2A",  # stage
    "PIVOT",  # path
    87.5,  # fundamental_score
    100.0,  # trend_template_score
    70.0,  # momentum_score
    0.0,  # structure_score
    13.16,  # latest_close
    9.79,  # sma_50
    6.27,  # sma_200
    14.43,  # high_52w
    3.08,  # low_52w
    7,  # fund_pass_count
    11,  # tech_pass_count
)


class _Cursor:
    def __init__(self, world: _World) -> None:
        self._w = world
        self._row: Any = None

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        if "stock_signal_sepa_daily" in sql:
            self._row = self._w.sepa
        elif "option_metric_iv_percentile_daily" in sql:
            self._row = self._w.option
        elif "candidate_outcome" in sql:
            self._rows = self._w.outcome
            self._row = None
        else:  # pragma: no cover
            self._row = None

    def fetchone(self) -> Any:
        return self._row

    def fetchall(self) -> list[tuple]:
        return getattr(self, "_rows", [])


class _World:
    def __init__(
        self,
        *,
        sepa: Any = _SEPA_ROW,
        option: Any = (None, None, None),
        outcome: list[tuple] | None = None,
    ) -> None:
        self.sepa = sepa
        self.option = option
        self.outcome = outcome or []
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def rollback(self) -> None:
        self.rollbacks += 1


# ------------------ selection & invalidation ------------------


def test_selection_explains_why_the_symbol_was_picked() -> None:
    e = ev.build_candidate_evidence(_World(), "PAYS", source="harness", min_score=70.0)
    sel = e["selection"]
    assert sel["status"] == "ok"
    assert sel["sepa_score"] == 82.75
    assert sel["path"] == "PIVOT"
    assert sel["components"]["trend_template"] == 100.0


def test_invalidation_names_conditions_that_would_flip_the_call() -> None:
    e = ev.build_candidate_evidence(_World(), "PAYS", source="harness", min_score=70.0)
    joined = " ".join(e["invalidation"])
    assert "70" in joined
    assert "PIVOT" in joined
    assert "50-day" in joined


def test_price_context_positions_against_the_52_week_high() -> None:
    e = ev.build_candidate_evidence(_World(), "PAYS", source="harness")
    assert e["price_context"]["pct_off_52w_high"] == -8.8


# ------------------ absence must be explicit ------------------


def test_missing_option_analytics_says_so_rather_than_going_blank() -> None:
    e = ev.build_candidate_evidence(_World(option=(None, None, None)), "PAYS")
    oa = e["option_analytics"]
    assert oa["status"] == ev.NOT_MEASURED
    assert "option data" in oa["reason"]
    assert "iv_rank_1y" not in oa


def test_present_option_analytics_is_reported() -> None:
    e = ev.build_candidate_evidence(_World(option=(17.75, 1.2e9, "range")), "NVDA")
    oa = e["option_analytics"]
    assert oa["status"] == "ok"
    assert oa["iv_rank_1y"] == 17.75
    assert oa["terrain_regime"] == "range"


def test_missing_sepa_row_is_not_measured_not_a_zero_score() -> None:
    e = ev.build_candidate_evidence(_World(sepa=None), "ZZZZ")
    assert e["selection"]["status"] == ev.NOT_MEASURED
    assert "sepa_score" not in e["selection"]


def test_empty_ledger_is_not_a_zero_hit_rate() -> None:
    e = ev.build_candidate_evidence(_World(outcome=[]), "PAYS", source="harness")
    tr = e["track_record"]
    assert tr["status"] == ev.NOT_MEASURED
    assert "not a zero hit rate" in tr["reason"]


def test_settled_ledger_reports_the_rate() -> None:
    e = ev.build_candidate_evidence(
        _World(outcome=[(5, 10, 6, 0.012)]), "PAYS", source="harness"
    )
    tr = e["track_record"]
    assert tr["status"] == "ok"
    assert tr["horizons"][0]["hit_rate"] == 0.6


def test_unreadable_option_row_degrades_instead_of_raising() -> None:
    """A driver returning something unexpected is a fact about our read."""
    e = ev.build_candidate_evidence(_World(option=object()), "PAYS")
    assert e["option_analytics"]["status"] == ev.NOT_MEASURED


# ------------------ the plan controls it ------------------


def test_analyze_symbol_is_a_valid_op() -> None:
    assert OP_ANALYZE_SYMBOL in VALID_OPS


def test_runtime_reads_the_plan_rather_than_ignoring_it() -> None:
    """Guards the W4 change itself: plan_ops must gate evidence assembly.

    Before this, run_objective executed a fixed sequence and never consulted the
    plan, so an LLM planner could describe steps but not cause them.
    """
    import inspect

    from bifrost_research.copilot.harness import runtime

    src = inspect.getsource(runtime.run_objective)
    assert "plan_ops" in src
    assert "want_evidence = OP_ANALYZE_SYMBOL in plan_ops" in src
