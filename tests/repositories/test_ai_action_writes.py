"""writes_by_sessions — Desk Threads Writes column aggregation."""

from __future__ import annotations

from bifrost_research.repositories import ai_action_log as action_repo


class _FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.sql: str | None = None
        self.params: tuple | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, rows: list[tuple]) -> None:
        self.cursor_obj = _FakeCursor(rows)

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj


def test_writes_by_sessions_empty_input() -> None:
    assert action_repo.writes_by_sessions(_FakeConn([]), []) == {}


def test_writes_by_sessions_excludes_non_write_kinds() -> None:
    conn = _FakeConn([])
    action_repo.writes_by_sessions(conn, ["sid-a"])
    assert "action_kind <> ALL" in (conn.cursor_obj.sql or "")
    assert conn.cursor_obj.params[0] == ["sid-a"]
    assert set(conn.cursor_obj.params[1]) == set(action_repo._NON_WRITE_KINDS)


def test_writes_by_sessions_groups_by_status() -> None:
    conn = _FakeConn(
        [
            ("sid-a", "proposed", 2),
            ("sid-a", "executed", 1),
            ("sid-b", "rejected", 3),
        ]
    )
    out = action_repo.writes_by_sessions(conn, ["sid-a", "sid-b"])
    assert out == {
        "sid-a": {"proposed": 2, "executed": 1},
        "sid-b": {"rejected": 3},
    }
    assert conn.cursor_obj.params[0] == ["sid-a", "sid-b"]
    assert set(conn.cursor_obj.params[1]) == set(action_repo._NON_WRITE_KINDS)


def test_cost_by_sessions_sums() -> None:
    conn = _FakeConn([("sid-a", 0.0123), ("sid-b", 1.5)])
    out = action_repo.cost_by_sessions(conn, ["sid-a", "sid-b"])
    assert out == {"sid-a": 0.0123, "sid-b": 1.5}
    assert conn.cursor_obj.params == (["sid-a", "sid-b"],)


def test_cost_by_sessions_empty() -> None:
    assert action_repo.cost_by_sessions(_FakeConn([]), []) == {}


def test_spend_today_chat_turns_shape() -> None:
    conn = _FakeConn([(1.25, 900)])
    out = action_repo.spend_today_chat_turns(conn)
    assert out == {"cost_usd": 1.25, "tokens": 900}
