"""set_origin_once — first-turn client_context sticks; later turns do not overwrite."""

from __future__ import annotations

from bifrost_research.repositories import copilot_session as session_repo


class _FakeCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.statements.append((sql, params or ()))


class _FakeConn:
    def __init__(self) -> None:
        self.cursor_obj = _FakeCursor()
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1


def test_set_origin_once_noops_when_all_empty() -> None:
    conn = _FakeConn()
    session_repo.set_origin_once(conn, "00000000-0000-0000-0000-000000000001")
    assert conn.cursor_obj.statements == []
    assert conn.commits == 0


def test_set_origin_once_writes_coalesce_and_uppercases_symbol() -> None:
    conn = _FakeConn()
    sid = "00000000-0000-0000-0000-000000000001"
    session_repo.set_origin_once(
        conn,
        sid,
        origin_page="/research/symbol",
        origin_label="Symbol · overview",
        origin_symbol="nvda",
    )
    assert len(conn.cursor_obj.statements) == 1
    sql, params = conn.cursor_obj.statements[0]
    assert "COALESCE(origin_page" in sql
    assert params == ("/research/symbol", "Symbol · overview", "NVDA", sid)
    assert conn.commits == 1
