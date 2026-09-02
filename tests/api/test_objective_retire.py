"""Retiring an objective must not take its history with it.

`objective_run.objective_id` has no ON DELETE clause, so Postgres refuses to
delete an objective that ever ran — and candidates carry the objective id in
`source_ref`, so the lineage would dangle even if it did not. Archiving is the
retirement path; delete is only for one that never produced anything.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from bifrost_research.api import harness as api
from bifrost_research.repositories import objective as obj_repo

OBJ = {"id": "obj-x", "title": "T", "status": "active"}


class _Conn:
    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(api, "_connect_or_503", lambda: _Conn())


def test_archiving_keeps_the_objective_row(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _set(conn: Any, oid: str, *, status: str) -> dict[str, Any]:
        seen["id"], seen["status"] = oid, status
        return {**OBJ, "status": status}

    monkeypatch.setattr(obj_repo, "set_objective_status", _set)
    out = api.set_objective_status("obj-x", api.ObjectiveStatusPatch(status="archived"))
    assert out["data"]["status"] == "archived"
    assert seen == {"id": "obj-x", "status": "archived"}


def test_archiving_an_unknown_objective_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obj_repo, "set_objective_status", lambda *a, **k: None)
    with pytest.raises(HTTPException) as e:
        api.set_objective_status("nope", api.ObjectiveStatusPatch(status="archived"))
    assert e.value.status_code == 404


def test_delete_is_refused_once_it_has_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(obj_repo, "get_objective", lambda conn, oid: OBJ)
    monkeypatch.setattr(obj_repo, "count_runs", lambda conn, oid: 12)
    monkeypatch.setattr(
        obj_repo, "delete_objective", lambda conn, oid: deleted.append(oid) or True
    )

    with pytest.raises(HTTPException) as e:
        api.delete_objective("obj-x")

    assert e.value.status_code == 409
    assert "12 run(s)" in str(e.value.detail)
    # Names the alternative rather than only refusing.
    assert "Archive" in str(e.value.detail)
    assert deleted == [], "refused delete must not reach the repository"


def test_delete_is_allowed_when_nothing_was_ever_produced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(obj_repo, "get_objective", lambda conn, oid: OBJ)
    monkeypatch.setattr(obj_repo, "count_runs", lambda conn, oid: 0)
    monkeypatch.setattr(
        obj_repo, "delete_objective", lambda conn, oid: bool(deleted.append(oid)) or True
    )
    out = api.delete_objective("obj-x")
    assert out["data"]["deleted"] is True
    assert deleted == ["obj-x"]


def test_deleting_an_unknown_objective_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obj_repo, "get_objective", lambda conn, oid: None)
    with pytest.raises(HTTPException) as e:
        api.delete_objective("nope")
    assert e.value.status_code == 404


def test_archived_objectives_leave_the_default_listing() -> None:
    """The console lists active objectives, so archiving is what retires one."""
    import inspect

    src = inspect.getsource(obj_repo.list_objectives)
    assert 'status: str | None = "active"' in src
    assert obj_repo.ARCHIVED_STATUS != "active"


# ------------------ runs ------------------

RUN = {"id": "run-1", "objective_id": "obj-x", "status": "completed"}


def test_run_delete_is_refused_while_candidates_point_at_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing in the schema stops this — no foreign key targets objective_run.

    Candidates carry the run in `source_ref`, a jsonb field the database will not
    defend, so the delete would succeed and quietly leave the Inbox card and the
    outcome ledger pointing at a run that no longer exists.
    """
    deleted: list[str] = []
    monkeypatch.setattr(obj_repo, "get_run", lambda conn, rid: RUN)
    monkeypatch.setattr(obj_repo, "count_candidates_for_run", lambda conn, rid: 8)
    monkeypatch.setattr(
        obj_repo, "delete_run", lambda conn, rid: bool(deleted.append(rid)) or True
    )

    with pytest.raises(HTTPException) as e:
        api.delete_run("run-1")

    assert e.value.status_code == 409
    assert "8 candidate(s)" in str(e.value.detail)
    assert deleted == [], "refused delete must not reach the repository"


def test_run_delete_is_allowed_once_nothing_references_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(obj_repo, "get_run", lambda conn, rid: RUN)
    monkeypatch.setattr(obj_repo, "count_candidates_for_run", lambda conn, rid: 0)
    monkeypatch.setattr(
        obj_repo, "delete_run", lambda conn, rid: bool(deleted.append(rid)) or True
    )
    out = api.delete_run("run-1")
    assert out["data"]["deleted"] is True
    assert deleted == ["run-1"]


def test_deleting_an_unknown_run_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obj_repo, "get_run", lambda conn, rid: None)
    with pytest.raises(HTTPException) as e:
        api.delete_run("nope")
    assert e.value.status_code == 404


def test_archiving_is_reversible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Archive must not be a one-way door — the same endpoint restores.

    The console lists active objectives, so an archived one is invisible; a
    restore path is what keeps archiving a retirement rather than a deletion in
    disguise.
    """
    seen: list[str] = []
    monkeypatch.setattr(
        obj_repo,
        "set_objective_status",
        lambda conn, oid, *, status: seen.append(status) or {**OBJ, "status": status},
    )
    api.set_objective_status("obj-x", api.ObjectiveStatusPatch(status="archived"))
    out = api.set_objective_status("obj-x", api.ObjectiveStatusPatch(status="active"))
    assert seen == ["archived", "active"]
    assert out["data"]["status"] == "active"


class _CountCursor:
    """Records the SQL so the lineage query is actually executed, not stubbed."""

    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    def __enter__(self) -> "_CountCursor":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._store["sql"] = sql
        self._store["params"] = params

    def fetchone(self) -> tuple:
        return (3,)


class _CountConn:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def cursor(self) -> _CountCursor:
        return _CountCursor(self.store)


def test_lineage_count_runs_for_real() -> None:
    """Exercises the query rather than stubbing it.

    Every other test here monkeypatches count_candidates_for_run, which is how a
    missing table-name import survived them — ruff caught the undefined name, the
    suite did not.
    """
    conn = _CountConn()
    assert obj_repo.count_candidates_for_run(conn, "run-1") == 3
    assert "source_ref ->> 'run_id'" in conn.store["sql"]
    assert "candidate_pool" in conn.store["sql"]
    assert conn.store["params"] == ("run-1",)
