"""A stage with no time is a stage you cannot account for.

The console rendered six green stages and one total duration, so a five-second
run looked the same whether the time went to the universe scan or the personas.
"""

from __future__ import annotations

from bifrost_research.copilot.harness.trace import RunTrace


def test_every_appended_event_is_stamped() -> None:
    t = RunTrace()
    t.append({"step": "plan"})
    t.append({"step": "scan_universe"})
    assert all("at_ms" in e for e in t)


def test_stamps_do_not_go_backwards() -> None:
    t = RunTrace()
    for step in ("plan", "scan_universe", "propose_candidates", "draft_candidate_batch"):
        t.append({"step": step})
    marks = [e["at_ms"] for e in t]
    assert marks == sorted(marks), marks


def test_the_first_event_starts_near_zero() -> None:
    # Elapsed since the run began, not wall clock — a reader asking "where did
    # the time go" cannot use an absolute timestamp without doing the
    # subtraction themselves.
    t = RunTrace()
    t.append({"step": "plan"})
    assert t[0]["at_ms"] < 1000


def test_a_caller_with_a_better_time_keeps_it() -> None:
    t = RunTrace()
    t.append({"step": "scan_universe", "at_ms": 4242})
    assert t[0]["at_ms"] == 4242


def test_it_is_still_a_list_of_events() -> None:
    # run_objective passes this straight into trace_json and the API serialises
    # it; anything that is not list-shaped breaks the wire format.
    t = RunTrace()
    t.append({"step": "plan"})
    assert isinstance(t, list)
    assert list(t) == [{"step": "plan", "at_ms": t[0]["at_ms"]}]
