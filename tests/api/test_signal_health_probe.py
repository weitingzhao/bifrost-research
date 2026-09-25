"""A probe that did not finish is not a missing table (0.114.0), and the payload is cached."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Self

import pytest

from bifrost_research.api import signal_health as sh


class _Cursor:
    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self._last: Any = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
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

    def cursor(self) -> _Cursor:
        return _Cursor(self.script)

    def rollback(self) -> None:
        self.rollbacks += 1


def test_a_cancelled_scan_is_unprobed_with_no_count() -> None:
    conn = _Conn([(True, True), RuntimeError("canceling statement due to statement timeout")])
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


def test_an_answered_probe_reads_its_age() -> None:
    at = datetime.now(UTC)
    out = sh._table_freshness(_Conn([(True, True), (2_312_255, at)]), "canonical_pnl", "features.t")
    assert out["status"] == "fresh"
    assert out["row_count"] == 2_312_255


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
