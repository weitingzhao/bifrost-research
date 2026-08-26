"""Smoke tests for DDL helpers (no live DB connection needed).

Uses a fake psycopg2-style cursor/connection to verify the DDL emits
the expected CREATE TABLE / CREATE INDEX statements. Marked without
``db`` so it runs in the default ``make test`` suite.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.schema.ddl import (
    apply_research_workflow_ddl,
    _create_research_workflow_tables,
)
from bifrost_research.schema.schemas import (
    SCHEMA_RESEARCH,
    TABLE_RESEARCH_AI_ACTION_LOG,
    TABLE_RESEARCH_AI_DRAFT,
    TABLE_RESEARCH_HYPOTHESIS,
)


class _FakeCursor:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def execute(self, query: str, params: Any = None) -> None:
        self._sink.append(query)

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.committed = 0
        self.rolled_back = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.statements)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


def _joined(conn: _FakeConnection) -> str:
    return "\n".join(conn.statements)


def test_apply_research_workflow_ddl_creates_hypothesis_table() -> None:
    conn = _FakeConnection()
    apply_research_workflow_ddl(conn)
    body = _joined(conn)
    assert f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_RESEARCH}" in body
    assert f"CREATE TABLE IF NOT EXISTS {TABLE_RESEARCH_HYPOTHESIS}" in body
    assert conn.committed == 1


def test_hypothesis_table_has_expected_columns() -> None:
    statements: list[str] = []
    cur = _FakeCursor(statements)
    _create_research_workflow_tables(cur)
    body = "\n".join(statements)
    expected_columns = (
        "id",
        "title",
        "thesis",
        "symbols",
        "tags",
        "status",
        "origin_page",
        "origin_ref",
        "linked_opportunity_ids",
        "linked_backtest_ids",
        "conclusion",
        "created_at",
        "updated_at",
        "retired_at",
    )
    for column in expected_columns:
        assert column in body, f"missing column {column} in hypothesis DDL"


def test_hypothesis_table_has_expected_indexes() -> None:
    statements: list[str] = []
    cur = _FakeCursor(statements)
    _create_research_workflow_tables(cur)
    body = "\n".join(statements)
    assert "hypothesis_status" in body
    assert "USING GIN (symbols)" in body
    assert "hypothesis_symbols" in body
    assert "hypothesis_updated" in body


def test_schema_constants_match_plan_wave_rs_a() -> None:
    assert SCHEMA_RESEARCH == "research"
    assert TABLE_RESEARCH_HYPOTHESIS == "research.hypothesis"


def test_ai_action_log_and_draft_tables_in_workflow_ddl() -> None:
    statements: list[str] = []
    cur = _FakeCursor(statements)
    _create_research_workflow_tables(cur)
    body = "\n".join(statements)
    assert f"CREATE TABLE IF NOT EXISTS {TABLE_RESEARCH_AI_ACTION_LOG}" in body
    assert f"CREATE TABLE IF NOT EXISTS {TABLE_RESEARCH_AI_DRAFT}" in body
    for col in (
        "action_kind",
        "action_source",
        "tool_calls",
        "approved_by",
        "executed_result",
    ):
        assert col in body, f"missing ai_action_log column {col}"
    for col in ("kind", "payload", "scope", "generated_by", "linked_action_id", "expires_at"):
        assert col in body, f"missing ai_draft column {col}"
    assert "ai_action_log_status" in body
    assert "ai_action_log_source" in body
    assert "ai_draft_pending" in body
    assert "ai_draft_scope" in body


def test_schema_constants_match_plan_wave_rs_e3() -> None:
    assert TABLE_RESEARCH_AI_ACTION_LOG == "research.ai_action_log"
    assert TABLE_RESEARCH_AI_DRAFT == "research.ai_draft"
