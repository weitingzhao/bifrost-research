"""Deleting a run must not destroy what the market already told us.

`candidate_outcome.candidate_id` cascades on delete, so removing a run's
candidates removed their settled outcomes with them. Those rows are the only
evidence the leash, the weekly policy review and the Autopilot's track record
read; after one round of run cleanup the ledger held three rows and the leash's
source-record gate could not open on any candidate.
"""

from __future__ import annotations

from typing import Any, Self

from bifrost_research.repositories import objective as obj_repo

RUN = {"id": "run-1", "outputs": {"draft_ids": ["drf-1"]}}


class _Cur:
    """Records every statement; answers the one SELECT the delete path makes."""

    def __init__(self, log: list[tuple[str, tuple]]) -> None:
        self.log = log
        self.rowcount = 0
        self._row: tuple | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.log.append((" ".join(sql.split()), params))
        if sql.lstrip().upper().startswith("SELECT COUNT"):
            self._row = (2,)
            self.rowcount = 1
        elif "DELETE FROM" in sql and "candidate_pool" in sql:
            self.rowcount = 6
        else:
            self.rowcount = 1

    def fetchone(self) -> tuple | None:
        return self._row


class _Conn:
    def __init__(self) -> None:
        self.log: list[tuple[str, tuple]] = []
        self.committed = False

    def cursor(self) -> _Cur:
        return _Cur(self.log)

    def commit(self) -> None:
        self.committed = True


def _force(monkeypatch) -> tuple[dict[str, Any], _Conn]:
    conn = _Conn()
    monkeypatch.setattr(obj_repo, "get_run", lambda _c, _r: RUN)
    return obj_repo.force_delete_run(conn, "run-1"), conn


def _stmt(conn: _Conn, needle: str) -> str:
    for sql, _params in conn.log:
        if needle in sql:
            return sql
    raise AssertionError(f"no statement containing {needle!r}; got {[s for s, _ in conn.log]}")


def test_delete_skips_candidates_that_have_a_settled_outcome(monkeypatch) -> None:
    _out, conn = _force(monkeypatch)
    delete = _stmt(conn, "DELETE FROM research.candidate_pool")
    assert "NOT EXISTS" in delete
    assert "research.candidate_outcome" in delete
    assert "o.candidate_id = c.id" in delete


def test_it_reports_what_it_kept_as_well_as_what_it_removed(monkeypatch) -> None:
    out, _conn = _force(monkeypatch)
    assert out["candidates_removed"] == 6
    assert out["candidates_kept"] == 2
    assert out["deleted"] is True
    assert out["force"] is True


def test_both_candidate_statements_are_scoped_to_the_run(monkeypatch) -> None:
    _out, conn = _force(monkeypatch)
    for needle in ("SELECT count(*) FROM research.candidate_pool", "DELETE FROM research.candidate_pool"):
        sql = _stmt(conn, needle)
        assert "source_ref ->> 'run_id' = %s" in sql
    for sql, params in conn.log:
        if "candidate_pool" in sql:
            assert params == ("run-1",), sql


def test_the_run_row_still_goes_and_the_work_is_committed(monkeypatch) -> None:
    out, conn = _force(monkeypatch)
    assert _stmt(conn, "DELETE FROM research.objective_run")
    assert conn.committed
    assert out["id"] == "run-1"
