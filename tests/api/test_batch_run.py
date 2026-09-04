"""Batch-run + Trust gate API — Wave B."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from bifrost_research.api import harness as api
from bifrost_research.repositories import objective as obj_repo

OBJ = {
    "id": "obj-x",
    "title": "T",
    "status": "active",
    "owner_id": "owner",
    "policy_json": {},
}


class _Conn:
    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(api, "_connect_or_503", lambda: _Conn())


def test_batch_run_starts_async_and_returns_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP batch-run must return immediately with a running row for Pipeline."""
    started: list[bool] = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None) -> None:  # noqa: ANN001
            self._target = target

        def start(self) -> None:
            started.append(True)

    monkeypatch.setattr(obj_repo, "get_objective", lambda conn, oid: OBJ)
    monkeypatch.setattr(
        obj_repo,
        "create_run",
        lambda conn, **kw: {
            "id": "run-async",
            "status": "running",
            "objective_id": "obj-x",
            "plan_json": kw.get("plan_json") or {},
        },
    )
    monkeypatch.setattr(obj_repo, "patch_run_trace", lambda *a, **k: None)
    monkeypatch.setattr(
        "bifrost_research.copilot.harness.runtime._heuristic_plan",
        lambda obj: {"steps": [{"op": "scan_universe"}], "generated_by": "heuristic"},
    )
    monkeypatch.setattr(
        "bifrost_research.copilot.harness.batch_orchestrate.trust_status",
        lambda: {"l0": False, "reason": "not L0", "skill": "research-loop-batch"},
    )
    monkeypatch.setattr("threading.Thread", _FakeThread)

    out = api.batch_run_objective("obj-x", api.BatchRunBody(curate_after=True))
    assert out["data"]["started"] is True
    assert out["data"]["run"]["id"] == "run-async"
    assert out["data"]["run"]["status"] == "running"
    assert started == [True]


def test_batch_run_refuses_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        obj_repo,
        "get_objective",
        lambda conn, oid: {**OBJ, "status": "archived"},
    )
    with pytest.raises(HTTPException) as e:
        api.batch_run_objective("obj-x")
    assert e.value.status_code == 409


def test_batch_run_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obj_repo, "get_objective", lambda conn, oid: None)
    with pytest.raises(HTTPException) as e:
        api.batch_run_objective("missing")
    assert e.value.status_code == 404


def test_trust_endpoint_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    import bifrost_research.copilot.harness.batch_orchestrate as bo

    monkeypatch.setattr(
        bo,
        "trust_status",
        lambda: {
            "skill": "research-loop-batch",
            "batch_mode_env": False,
            "l0": False,
            "reason": "off",
            "advisory": "D10",
        },
    )
    out = api.get_loop_trust()
    assert out["data"]["l0"] is False
    assert out["data"]["skill"] == "research-loop-batch"


def test_process_objective_skips_approve_when_not_l0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bifrost_research.copilot.harness import batch_orchestrate as bo

    calls: list[str] = []

    monkeypatch.setattr(
        bo,
        "run_objective",
        lambda conn, *, objective, existing_run=None: {
            "run": {"id": "run-1", "status": "awaiting_approval"},
            "outputs": {},
        },
    )
    monkeypatch.setattr(bo, "trust_l0_research_loop_batch", lambda: False)
    monkeypatch.setattr(
        bo,
        "trust_status",
        lambda: {"l0": False, "reason": "not L0", "skill": "research-loop-batch"},
    )
    monkeypatch.setattr(
        bo,
        "run_curator_for_run",
        lambda *a, **k: calls.append("curate") or {"status": "ok"},
    )
    monkeypatch.setattr(
        bo,
        "approve_all_for_run",
        lambda *a, **k: calls.append("approve") or {"count": 0},
    )
    monkeypatch.setattr(bo.obj_repo, "append_run_trace_event", lambda *a, **k: None)
    monkeypatch.setattr(bo.obj_repo, "get_run", lambda *a, **k: {"id": "run-1"})
    monkeypatch.setattr(bo.obj_repo, "patch_run_outputs", lambda *a, **k: None)

    result = bo.process_objective(
        _Conn(),
        OBJ,
        curate_after=True,
        batch_mode=True,
    )
    assert "approve" not in calls
    assert "curate" in calls
    assert result.get("approve_skipped") is True
