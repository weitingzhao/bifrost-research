"""An objective has two states, and a third would be invisible.

`_ALLOWED_OBJ_STATUSES` named `active`, `paused` and `retired`, had no readers,
and was never enforced. The word the system actually writes is `archived` — the
API archives with it, the console filters on it, and the frontend's own type
disagreed with all three. A status outside the pair disappears twice: the
console lists `active`, and `?status=archived` will not find it either.

Kept in step with `bifrost-trade-frontend/src/api/research/harness.ts`
(`OBJECTIVE_STATUSES`).
"""

from __future__ import annotations

from typing import Any

import pytest

from bifrost_research.repositories import objective as obj_repo


class _Cur:
    def __init__(self, row: tuple | None) -> None:
        self.row = row
        self.executed: list[tuple] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple | None:
        return self.row


class _Conn:
    def __init__(self, row: tuple | None = None) -> None:
        self._cur = _Cur(row)
        self.commits = 0

    def cursor(self) -> _Cur:
        return self._cur

    def commit(self) -> None:
        self.commits += 1


def test_the_vocabulary_is_the_two_words_the_system_writes() -> None:
    assert obj_repo.OBJECTIVE_STATUSES == frozenset({"active", "archived"})


@pytest.mark.parametrize("status", ["paused", "retired", "archivd", "ARCHIVED", ""])
def test_an_unrecognised_status_is_refused_before_it_is_written(status: str) -> None:
    conn = _Conn()
    with pytest.raises(ValueError) as exc:
        obj_repo.set_objective_status(conn, "obj-x", status=status)
    assert "invalid objective status" in str(exc.value)
    assert conn.commits == 0
    assert conn._cur.executed == []


@pytest.mark.parametrize("status", ["active", "archived"])
def test_both_real_statuses_reach_the_update(status: str) -> None:
    conn = _Conn(row=None)
    assert obj_repo.set_objective_status(conn, "obj-x", status=status) is None
    assert conn._cur.executed and conn._cur.executed[0][1] == (status, "obj-x")
