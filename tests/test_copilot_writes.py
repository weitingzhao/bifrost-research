"""The chat's writes, one row each — the Copilot Desk's Writes table."""

from __future__ import annotations

from datetime import date
from typing import Any, Self

import pytest
from fastapi.testclient import TestClient

from bifrost_research.copilot import writes
from bifrost_research.mcp.tools._write_common import WRITE_TOOL_NAMES
from bifrost_research.repositories import ai_action_log


class _Cursor:
    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self._current: Any = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((sql, params))
        self._current = self._results.pop(0) if self._results else None

    def fetchall(self) -> Any:
        return self._current

    def fetchone(self) -> Any:
        return self._current


class _Conn:
    def __init__(self, results: list[Any]) -> None:
        self.cur = _Cursor(results)

    def cursor(self) -> _Cursor:
        return self.cur

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


# ── The one-line change ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        (
            "research.loop.propose_candidate",
            {"symbol": "nvda", "score": 62.0, "source": "copilot"},
            "Candidate NVDA · score 62",
        ),
        ("research.loop.run_objective", {"objective_id": "obj-daily-loop-stock"}, "Run objective obj-daily-loop-stock"),
        (
            "research.hypothesis.patch",
            {"hypothesis_id": "h-2291", "status": "retired", "thesis": "tighter"},
            "h-2291 · thesis, status → retired",
        ),
        (
            "research.hypothesis.create",
            {"title": "PLTR sell-vol edge", "thesis": "…", "symbols": ["PLTR"]},
            "New hypothesis: PLTR sell-vol edge · PLTR",
        ),
        (
            "research.backtest.run_event_query",
            {"strategy_template": "short_straddle", "event_kind": "earnings", "lookback_years": 3},
            "Backtest short_straddle on earnings · 3y",
        ),
        ("research.playbook.propose_note", {"note_md": "# \nRoll early when IV > 60"}, "Note: Roll early when IV > 60"),
        (
            "research.loop.propose_order_intent",
            {"hypothesis_id": "h-1", "strategy_template": "csp", "legs": [{}, {}]},
            "csp intent for h-1 · 2 legs",
        ),
        ("research.loop.draft_decision", {"hypothesis_id": "h-1", "verdict": "go"}, "Go on h-1"),
    ],
)
def test_summarize_change_reads_the_arguments(tool: str, args: dict[str, Any], expected: str) -> None:
    assert writes.summarize_change(tool, args) == expected


def test_every_write_tool_has_a_kind_noun() -> None:
    # A new write tool must say what it is, not fall back to its method name.
    assert all(writes.kind_noun(t) in writes._KIND_NOUN.values() for t in WRITE_TOOL_NAMES)
    assert set(writes._KIND_NOUN) == set(WRITE_TOOL_NAMES)


def test_unknown_tool_falls_back_and_clips() -> None:
    out = writes.summarize_change("research.x.new_thing", {"symbol": "amd", "a": 1, "dry_run": True})
    assert out == "new thing · AMD · symbol, a"
    long = writes.summarize_change("research.hypothesis.create", {"title": "x" * 400})
    assert len(long) == writes.SUMMARY_MAX and long.endswith("…")


# ── One row ────────────────────────────────────────────────────────────────


def test_shape_write_keeps_the_thread_and_the_outcome() -> None:
    row = {
        "id": "aal_1",
        "action_kind": "research.loop.propose_candidate",
        "input": {"arguments": {"symbol": "MSFT"}, "tool_name": "research.loop.propose_candidate"},
        "status": "executed",
        "executed_result": {"ok": True, "data": {"result": {"id": "cand-msft"}}},
        "session_id": "0b8c1c9e-0000-0000-0000-000000000001",
        "thread_title": "MSFT after the print",
        "thread_status": "active",
        "created_at": "2026-09-26T14:00:00+00:00",
        "executed_at": "2026-09-26T14:00:01+00:00",
    }
    out = writes.shape_write(row)
    assert out == {
        "id": "aal_1",
        "tool": "research.loop.propose_candidate",
        "kind": "candidate",
        "change": "Candidate MSFT",
        "symbol": "MSFT",
        "session_id": "0b8c1c9e-0000-0000-0000-000000000001",
        "thread_title": "MSFT after the print",
        "thread_archived": False,
        "status": "executed",
        "ok": True,
        "error": None,
        "created_at": "2026-09-26T14:00:00+00:00",
        "executed_at": "2026-09-26T14:00:01+00:00",
    }


def test_shape_write_without_a_thread_and_with_an_error() -> None:
    row = {
        "id": "aal_2",
        "action_kind": "research.loop.run_objective",
        "input": {"arguments": {"objective_id": "obj-1"}},
        "status": "error",
        "executed_result": {"ok": False, "error": "objective not found"},
        "session_id": None,
        "thread_title": "stale join",
        "created_at": "2026-09-26T14:00:00+00:00",
    }
    out = writes.shape_write(row)
    assert out["session_id"] is None
    assert out["thread_title"] is None
    assert out["thread_archived"] is None
    assert out["ok"] is False
    assert out["error"] == "objective not found"


def test_window_start_includes_today() -> None:
    assert writes.window_start(1, today=date(2026, 9, 26)) == "2026-09-26"
    assert writes.window_start(7, today=date(2026, 9, 26)) == "2026-09-20"
    assert writes.window_start(0, today=date(2026, 9, 26)) == "2026-09-26"


# ── The SQL layer ──────────────────────────────────────────────────────────


def test_list_with_thread_scopes_and_counts() -> None:
    n = len(ai_action_log._COLUMNS)
    page_row = ["aal_1", "sid-1", "research.loop.run_objective", "user_chat"] + [None] * (n - 4)
    conn = _Conn([[tuple(page_row + ["A thread", "archived"])], (3,), ("2026-09-26T14:00:00+00:00",)])
    out = ai_action_log.list_with_thread(
        conn,
        action_source="user_chat",
        kinds=("research.loop.run_objective",),
        since_day="2026-09-20",
        owner_id="owner",
        limit=1,
    )
    page_sql, page_params = conn.cur.executed[0]
    assert "LEFT JOIN research.copilot_session s" in page_sql
    assert "s.owner_id = %s" in page_sql
    assert "a.approved_by = %s" in page_sql
    assert "ORDER BY a.created_at DESC" in page_sql
    # join owner, then source, kinds, owner, since, then the limit
    assert page_params == ("owner", "user_chat", ["research.loop.run_objective"], "owner", "2026-09-20", 1)
    count_sql, count_params = conn.cur.executed[1]
    assert "COUNT(*)" in count_sql and count_params == page_params[1:-1]
    last_sql, last_params = conn.cur.executed[2]
    assert "MAX(created_at)" in last_sql and "created_at >=" not in last_sql
    assert last_params == ("user_chat", ["research.loop.run_objective"], "owner")
    assert out["total"] == 3 and out["truncated"] is True
    assert out["last_at"] == "2026-09-26T14:00:00+00:00"
    assert out["rows"][0]["id"] == "aal_1"
    assert out["rows"][0]["thread_title"] == "A thread"
    assert out["rows"][0]["thread_status"] == "archived"


def test_count_by_status_bounds_the_day() -> None:
    conn = _Conn([[("executed", 2), ("rejected", 1)]])
    out = ai_action_log.count_by_status(
        conn,
        action_source="user_chat",
        kinds=WRITE_TOOL_NAMES,
        since_day="2026-09-26",
        until_day="2026-09-27",
        approved_by="owner",
    )
    sql, params = conn.cur.executed[0]
    assert "created_at >= (%s::date AT TIME ZONE 'UTC')" in sql
    assert "created_at < (%s::date AT TIME ZONE 'UTC')" in sql
    assert params == ("user_chat", list(WRITE_TOOL_NAMES), "owner", "2026-09-26", "2026-09-27")
    assert out == {"executed": 2, "rejected": 1}


# ── Counts and page, wired ─────────────────────────────────────────────────


def test_chat_write_counts_only_asks_for_chat_write_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(conn: Any, **kw: Any) -> dict[str, int]:
        seen.update(kw)
        return {"executed": 2, "expired": 4}

    monkeypatch.setattr(ai_action_log, "count_by_status", fake)
    out = writes.chat_write_counts(object(), day="2026-09-30", owner_id="owner")
    assert out == {"proposed": 0, "approved": 0, "executed": 2, "rejected": 0, "error": 0}
    assert seen["action_source"] == "user_chat"
    assert tuple(seen["kinds"]) == WRITE_TOOL_NAMES
    assert "chat_turn" not in seen["kinds"] and "draft_approve" not in seen["kinds"]
    assert (seen["since_day"], seen["until_day"]) == ("2026-09-30", "2026-10-01")


def test_chat_write_counts_is_fail_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(ai_action_log, "count_by_status", boom)
    assert writes.chat_write_counts(object(), day="2026-09-26") == dict.fromkeys(writes.STATUSES, 0)


def test_chat_writes_shapes_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(conn: Any, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {
            "rows": [{"id": "aal_1", "action_kind": "research.loop.run_objective", "input": {}, "status": "approved"}],
            "total": 1,
            "truncated": False,
            "last_at": "2026-09-07T05:34:33+00:00",
        }

    monkeypatch.setattr(ai_action_log, "list_with_thread", fake)
    out = writes.chat_writes(object(), owner_id="owner", days=7, limit=50)
    assert seen["owner_id"] == "owner" and seen["limit"] == 50
    assert seen["since_day"] == writes.window_start(7)
    assert out["days"] == 7 and out["total"] == 1 and out["truncated"] is False
    assert out["last_write_at"] == "2026-09-07T05:34:33+00:00"
    assert out["rows"][0]["kind"] == "objective"
    assert out["rows"][0]["change"] == "Run objective ?"


# ── The route ──────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("bifrost_research.api.health.run_startup_schema_guard", lambda: None)
    from bifrost_research.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def test_writes_route_returns_the_page(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import bifrost_research.api.copilot_writes as api

    class _C:
        def close(self) -> None: ...

    seen: dict[str, Any] = {}

    def fake(conn: Any, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"rows": [], "total": 0, "truncated": False, "last_at": None}

    monkeypatch.setattr(api, "connect", lambda: _C())
    monkeypatch.setattr(ai_action_log, "list_with_thread", fake)
    res = client.get("/research/copilot/writes", params={"days": 3, "limit": 10})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["db_ok"] is True and data["days"] == 3 and data["rows"] == []
    assert seen["limit"] == 10 and seen["since_day"] == writes.window_start(3)


def test_writes_route_bounds_and_db_down(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import bifrost_research.api.copilot_writes as api

    assert client.get("/research/copilot/writes", params={"days": 31}).status_code == 422
    assert client.get("/research/copilot/writes", params={"limit": 0}).status_code == 422

    def down() -> None:
        raise RuntimeError("no db")

    monkeypatch.setattr(api, "connect", down)
    data = client.get("/research/copilot/writes").json()["data"]
    assert data["db_ok"] is False and data["rows"] == [] and data["days"] == 7
