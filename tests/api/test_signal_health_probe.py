"""A probe that did not finish is not a missing table (0.114.0), and the payload is cached."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Self

import pytest

from bifrost_research.api import signal_health as sh


class _Cursor:
    def __init__(self, script: list[Any], sql: list[str]) -> None:
        self._script = script
        self._last: Any = None
        self.sql = sql

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql.append(sql)
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        self._last = step

    def fetchone(self) -> Any:
        return self._last


class _Conn:
    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.rollbacks = 0
        self.sql: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.script, self.sql)

    def rollback(self) -> None:
        self.rollbacks += 1


def test_a_cancelled_probe_is_unprobed_with_no_count() -> None:
    conn = _Conn([(True, True), (2_311_020, 0), RuntimeError("canceling statement due to statement timeout")])
    out = sh._table_freshness(conn, "canonical_pnl", "features.t")
    assert out["status"] == "unprobed"
    assert out["row_count"] is None
    assert "statement timeout" in out["error"]
    # The old code scanned the table a second time after the first failed.
    assert conn.script == []


def test_a_table_that_is_not_there_is_missing() -> None:
    out = sh._table_freshness(_Conn([(False, False)]), "x", "features.gone")
    assert out["status"] == "missing"
    assert out["row_count"] == 0


def test_an_answered_probe_reads_its_age_and_estimates_its_rows() -> None:
    at = datetime.now(UTC)
    conn = _Conn([(True, True), (2_311_020, 0), (at,)])
    out = sh._table_freshness(conn, "canonical_pnl", "features.t")
    assert out["status"] == "fresh"
    assert out["row_count"] == 2_311_020
    assert out["row_count_estimated"] is True
    assert conn.script == []  # three catalogue/index reads, no COUNT(*)
    assert not any("COUNT(*)::bigint FROM" in q for q in conn.sql)


def test_the_estimate_reads_a_plain_table_itself() -> None:
    """0.115.0 summed pg_partition_tree() only, which is empty for a plain table: every count read 0."""
    conn = _Conn([(True, True), (10_038, 0), (datetime.now(UTC),)])
    sh._table_freshness(conn, "scan", "features.t")
    estimate_sql = " ".join(conn.sql[1].split())
    assert "c.relkind <> 'p'" in estimate_sql
    assert "pg_partition_tree" in estimate_sql


def test_a_leaf_never_analysed_is_counted_exactly() -> None:
    at = datetime.now(UTC)
    conn = _Conn([(True, True), (0, 1), (143,), (at,)])
    out = sh._table_freshness(conn, "gex_intraday", "features.t")
    assert out["row_count"] == 143
    assert out["row_count_estimated"] is False


def test_unprobed_neither_degrades_nor_clears_the_roll_up() -> None:
    assert sh._overall_from_freshness([{"status": "fresh"}, {"status": "unprobed"}]) == "ok"
    assert sh._overall_from_freshness([{"status": "stale"}, {"status": "unprobed"}]) == "degraded"
    assert sh._overall_from_freshness([{"status": "unprobed"}, {"status": "unprobed"}]) == "unknown"
    assert sh._overall_from_freshness([{"status": "missing"}]) == "empty"


def _payload(status: str) -> dict[str, Any]:
    return {"ok": True, "data": {"freshness": [{"status": status}], "extra_tables": [], "canonical_pnl": {}}}


def test_the_payload_is_cached_and_a_partial_one_only_briefly(monkeypatch: pytest.MonkeyPatch) -> None:
    sh.reset_cache()
    calls: list[str] = []
    answers = iter([_payload("unprobed"), _payload("fresh")])

    def compute() -> dict[str, Any]:
        calls.append("x")
        return next(answers)

    clock = [1000.0]
    monkeypatch.setattr(sh, "_compute_signal_health", compute)
    monkeypatch.setattr(sh.time, "monotonic", lambda: clock[0])

    first = sh.signal_health()
    assert first["data"]["freshness"][0]["status"] == "unprobed"
    clock[0] += 10
    assert sh.signal_health() is first  # inside the partial window
    clock[0] += sh.PARTIAL_CACHE_TTL_SECONDS
    second = sh.signal_health()  # retried once the partial window closed
    assert second["data"]["freshness"][0]["status"] == "fresh"
    clock[0] += sh.CACHE_TTL_SECONDS - 1
    assert sh.signal_health() is second
    assert len(calls) == 2
    sh.reset_cache()
