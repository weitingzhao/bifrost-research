"""Editing what an objective is called, says, when it runs and who works it — in place."""

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
        self.row = ("obj-1", params[0], "d", "adhoc", {}, "loop_curator", "active", "owner", None, "assisted", None)

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


def test_mode_and_subject_are_edited_in_place_and_normalised():
    conn = _Conn()
    obj_repo.update_objective(conn, "obj-1", mode=" Hand ", subject=" rklb ")
    assert "SET mode = %s, subject = %s WHERE id = %s" in conn.store["sql"]
    assert conn.store["params"] == ("hand", "RKLB", "obj-1")


def test_an_empty_subject_clears_it():
    conn = _Conn()
    obj_repo.update_objective(conn, "obj-1", subject="  ")
    assert conn.store["params"] == (None, "obj-1")


def test_a_mode_outside_the_three_is_refused():
    with pytest.raises(ValueError, match="invalid mode"):
        obj_repo.update_objective(_Conn(), "obj-1", mode="semi")


def test_create_writes_mode_and_subject_with_assisted_as_the_default():
    conn = _Conn()
    obj_repo.create_objective(conn, title="Small-cap", description="d", objective_id="obj-x")
    assert conn.store["params"][-2:] == ("assisted", None)
    obj_repo.create_objective(conn, title="Small-cap", description="d", objective_id="obj-y", mode="hand", subject="rklb")
    assert conn.store["params"][-2:] == ("hand", "RKLB")
    row = obj_repo._obj_row(("obj-1", "t", "d", "adhoc", {}, "p", "active", "owner", None, "hand", "RKLB"))
    assert (row["mode"], row["subject"]) == ("hand", "RKLB")
