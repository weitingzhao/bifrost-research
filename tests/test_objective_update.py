"""Editing what an objective is called, says and when it runs — in place."""

from __future__ import annotations

import pytest

from bifrost_research.repositories import objective as obj_repo


class _Cur:
    def __init__(self, store):
        self.store = store
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.store["sql"] = " ".join(sql.split())
        self.store["params"] = params
        self.row = ("obj-1", params[0], "d", "adhoc", {}, "loop_curator", "active", "owner", None)

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self):
        self.store: dict = {}
        self.committed = False

    def cursor(self):
        return _Cur(self.store)

    def commit(self):
        self.committed = True


def test_update_sets_only_the_fields_given():
    conn = _Conn()
    row = obj_repo.update_objective(conn, "obj-1", title="  New title  ", schedule="Daily_Open")
    assert "SET title = %s, schedule = %s WHERE id = %s" in conn.store["sql"]
    assert conn.store["params"] == ("New title", "daily_open", "obj-1")
    assert conn.committed and row["title"] == "New title"


def test_update_refuses_a_schedule_the_cron_cannot_run():
    with pytest.raises(ValueError, match="invalid schedule"):
        obj_repo.update_objective(_Conn(), "obj-1", schedule="hourly")


def test_update_with_nothing_to_set_reads_rather_than_writes(monkeypatch):
    monkeypatch.setattr(obj_repo, "get_objective", lambda conn, oid: {"id": oid, "title": "unchanged"})
    conn = _Conn()
    assert obj_repo.update_objective(conn, "obj-1")["title"] == "unchanged"
    assert not conn.committed
