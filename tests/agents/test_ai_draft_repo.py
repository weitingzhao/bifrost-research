"""Unit tests for ai_action_log / ai_draft repository helpers (fake cursor)."""

from __future__ import annotations

import json
from typing import Any

from bifrost_research.repositories import ai_action_log as action_repo
from bifrost_research.repositories import ai_draft as draft_repo


class _FakeCursor:
    def __init__(self, conn: "_FakeConnection") -> None:
        self._conn = conn
        self._last: Any = None

    def execute(self, query: str, params: Any = None) -> None:
        self._conn.statements.append(query)
        self._conn.params.append(params)
        q = " ".join(query.split()).lower()
        if "insert into" in q and "ai_action_log" in q:
            row = {
                "id": params[0],
                "session_id": params[1],
                "action_kind": params[2],
                "action_source": params[3],
                "model": params[4],
                "input": params[5],
                "output": params[6],
                "tool_calls": params[7],
                "status": params[8],
                "approved_by": None,
                "approved_at": None,
                "executed_at": None,
                "executed_result": None,
                "created_at": "2026-08-25T00:00:00+00:00",
            }
            self._last = tuple(row[c] for c in action_repo._COLUMNS)
            self._conn.actions[params[0]] = row
        elif "insert into" in q and "ai_draft" in q:
            row = {
                "id": params[0],
                "kind": params[1],
                "payload": params[2],
                "scope": params[3],
                "status": params[4],
                "generated_by": params[5],
                "linked_action_id": params[6],
                "created_at": "2026-08-25T00:00:00+00:00",
                "expires_at": params[7],
            }
            self._last = tuple(row[c] for c in draft_repo._COLUMNS)
            self._conn.drafts[params[0]] = row
        elif "update" in q and "ai_draft" in q:
            did = params[1]
            st = params[0]
            row = self._conn.drafts.get(did)
            if row:
                row["status"] = st
                self._last = tuple(row[c] for c in draft_repo._COLUMNS)
            else:
                self._last = None
        elif "select count" in q:
            pending = sum(
                1 for d in self._conn.drafts.values() if d.get("status") == "pending"
            )
            self._last = (pending,)
        elif "select" in q and "ai_draft" in q and "where id" in q:
            did = params[0]
            row = self._conn.drafts.get(did)
            self._last = tuple(row[c] for c in draft_repo._COLUMNS) if row else None
        else:
            self._last = None

    def fetchone(self) -> Any:
        return self._last

    def fetchall(self) -> list[Any]:
        return []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: list[Any] = []
        self.actions: dict[str, dict[str, Any]] = {}
        self.drafts: dict[str, dict[str, Any]] = {}

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def test_insert_action_and_draft() -> None:
    conn = _FakeConnection()
    action = action_repo.insert_action(
        conn,
        action_kind="draft_hypothesis",
        action_source="morning_agent",
        model="heuristic",
        input_payload={"x": 1},
        output_payload={"y": 2},
    )
    assert action["id"].startswith("aal_")
    assert action["status"] == "proposed"
    draft = draft_repo.insert_draft(
        conn,
        kind="morning_brief",
        payload={"markdown": "hi"},
        scope="global",
        generated_by="morning_agent",
        linked_action_id=action["id"],
    )
    assert draft["id"].startswith("drf_")
    assert draft["status"] == "pending"
    assert isinstance(draft["payload"], (dict, str))
    if isinstance(draft["payload"], str):
        assert json.loads(draft["payload"])["markdown"] == "hi"


def test_update_draft_status() -> None:
    conn = _FakeConnection()
    draft = draft_repo.insert_draft(
        conn,
        kind="eod_verdict",
        payload={"proposed_status": "active"},
        scope="h1",
        generated_by="eod_agent",
    )
    updated = draft_repo.update_draft_status(conn, draft["id"], status="dismissed")
    assert updated is not None
    assert updated["status"] == "dismissed"
