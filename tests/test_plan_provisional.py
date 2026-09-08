"""A run started from the UI must plan the way the unattended run plans.

`start_async_batch` writes a heuristic plan so the run row exists before the
HTTP call returns and the Pipeline drawer can poll. The runtime then kept that
row's plan, so the LLM plan chain — shipped in 0.69.x and gated on
`policy.use_llm_plan` — never ran for a run started from the page. The same
objective behaved one way at 13:30 and another way under the Run now button.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.copilot.harness import batch_orchestrate
from bifrost_research.copilot.harness.planning import plan_needs_replanning

OBJ = {"id": "obj-a", "title": "A", "persona": "loop_curator", "policy_json": {"max_candidates": 3}}


def test_the_placeholder_is_replanned() -> None:
    assert plan_needs_replanning({"steps": [], "generated_by": "heuristic", "provisional": True})


def test_a_real_plan_is_kept() -> None:
    # Whatever made it, a plan that is not a placeholder is the run's plan.
    assert not plan_needs_replanning({"steps": [{"op": "scan_universe"}], "generated_by": "llm"})
    assert not plan_needs_replanning({"steps": [], "generated_by": "heuristic"})


def test_a_missing_or_malformed_plan_is_replanned() -> None:
    for empty in ({}, None, [], "plan"):
        assert plan_needs_replanning(empty), empty


class _Conn:
    def __init__(self) -> None:
        self.committed = False

    def cursor(self) -> Any:
        raise AssertionError("start_async_batch must not query beyond the repository fakes")

    def commit(self) -> None:
        self.committed = True


def test_start_async_batch_marks_its_placeholder(monkeypatch) -> None:
    created: dict[str, Any] = {}

    def fake_create_run(_conn, *, objective_id, plan_json):
        created["plan"] = plan_json
        created["objective_id"] = objective_id
        return {"id": "run-1", "objective_id": objective_id, "plan_json": plan_json}

    monkeypatch.setattr(batch_orchestrate.obj_repo, "create_run", fake_create_run)
    monkeypatch.setattr(batch_orchestrate.obj_repo, "patch_run_trace", lambda *a, **k: None)
    monkeypatch.setattr(batch_orchestrate, "trust_status", lambda: {"l0": False})
    # The background thread must not run: this test is about the row it starts
    # from, and `start_async_batch` imports threading inside the function.
    import threading

    monkeypatch.setattr(threading, "Thread", _NoThread)

    out = batch_orchestrate.start_async_batch(_Conn(), OBJ, curate_after=False)

    assert out["started"] is True
    assert created["plan"]["provisional"] is True, "the row must not be mistaken for a decision"
    assert created["plan"]["async_batch_start"] is True
    assert plan_needs_replanning(created["plan"])


class _NoThread:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    def start(self) -> None:
        return None
