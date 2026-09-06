"""B4 (research-loop-automation) — every objective runs; one failure is its own."""

from __future__ import annotations

from typing import Any

import pytest

from bifrost_research.copilot.harness import entry


class _Conn:
    def __init__(self) -> None:
        self.rollbacks = 0
        self.closed = False

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _objectives() -> list[dict[str, Any]]:
    return [
        {"id": "obj-stock", "title": "Daily Loop Stock Explorer", "schedule": "daily_open", "status": "active"},
        {"id": "obj-iv", "title": "Morning IV Hot Watch", "schedule": "daily_open", "status": "active"},
        {"id": "obj-weekly", "title": "Weekly", "schedule": "weekly", "status": "active"},
    ]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"conn": _Conn(), "ran": [], "reports": []}
    monkeypatch.setattr(entry, "connect", lambda: state["conn"])
    monkeypatch.setattr(entry.obj_repo, "list_objectives", lambda conn, **k: _objectives())
    monkeypatch.setattr(
        entry, "report_batch_outcome", lambda **kw: (state["reports"].append(kw) or True)
    )
    monkeypatch.delenv("BIFROST_LOOP_OBJECTIVE_ID", raising=False)
    return state


def _result(run_id: str, candidates: int) -> dict[str, Any]:
    return {
        "run": {"id": run_id, "status": "awaiting_approval"},
        "outputs": {"candidate_ids": [f"c{i}" for i in range(candidates)]},
        "approve_skipped": True,
    }


def test_a_broken_objective_does_not_stop_the_next_one(harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _process(conn, obj, *, curate_after, batch_mode):
        harness["ran"].append(obj["id"])
        if obj["id"] == "obj-stock":
            raise RuntimeError("scan table missing")
        return _result("run_iv", 3)

    monkeypatch.setattr(entry, "_process_objective", _process)

    code = entry.main(["--schedule=daily_open", "--batch-mode"])

    # Both daily_open objectives were attempted, the weekly one filtered out.
    assert harness["ran"] == ["obj-stock", "obj-iv"]
    assert harness["conn"].rollbacks == 1
    assert code == 0  # something ran; a retry would propose obj-iv's batch twice
    report = harness["reports"][-1]
    assert report["ok"] is False
    assert report["summary"] == "1/2 objectives ok; failed: obj-stock (RuntimeError: scan table missing)"
    assert harness["conn"].closed is True
    assert "run_iv" in capsys.readouterr().out


def test_everything_failing_is_an_exit_code(harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def _process(conn, obj, **kw):
        raise ValueError("no db")

    monkeypatch.setattr(entry, "_process_objective", _process)
    code = entry.main(["--schedule=daily_open", "--batch-mode"])
    assert code == 1
    assert harness["reports"][-1]["summary"].startswith("0/2 objectives ok; failed: obj-stock (ValueError: no db), obj-iv")


def test_all_good_reports_all_good(harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        entry, "_process_objective", lambda conn, obj, **kw: _result(f"run_{obj['id']}", 8 if obj["id"] == "obj-stock" else 3)
    )
    code = entry.main(["--schedule=daily_open", "--batch-mode"])
    assert code == 0
    assert harness["reports"][-1] == {"ok": True, "summary": "2/2 objectives ok"}


def test_env_pin_still_narrows_to_one_objective(harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_LOOP_OBJECTIVE_ID", "obj-iv")
    monkeypatch.setattr(entry.obj_repo, "get_objective", lambda conn, oid: next(o for o in _objectives() if o["id"] == oid))
    monkeypatch.setattr(entry, "_process_objective", lambda conn, obj, **kw: (harness["ran"].append(obj["id"]) or _result("r", 1)))
    assert entry.main(["--schedule=daily_open"]) == 0
    assert harness["ran"] == ["obj-iv"]


def test_summary_lines_say_what_happened() -> None:
    ok = entry.objective_outcome({"id": "obj-a", "title": "A"}, result=_result("run_1", 8))
    assert entry.summary_line(ok) == "objective obj-a (A): ok run=run_1 status=awaiting_approval candidates=8"
    approved = entry.objective_outcome(
        {"id": "obj-b", "title": "B"}, result={**_result("run_2", 2), "approve_result": {"n": 2}, "approve_skipped": False}
    )
    assert entry.summary_line(approved).endswith("candidates=2 auto-approved")
    failed = entry.objective_outcome({"id": "obj-c", "title": "C"}, error=RuntimeError("boom"))
    assert entry.summary_line(failed) == "objective obj-c (C): FAILED RuntimeError: boom"
    assert entry.batch_summary([ok, approved, failed]) == "2/3 objectives ok; failed: obj-c (RuntimeError: boom)"
