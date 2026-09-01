"""Fail-soft handlers must reset the connection, not just log.

Postgres aborts the whole transaction on the first failed statement. A handler
that catches the error, logs "returning empty" and hands the connection back has
not degraded gracefully — every later query raises InFailedSqlTransaction, and
the log says the error was contained. That is how a type mismatch in the events
layer took down an entire harness run while reporting itself as handled.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.copilot.harness import data_sources as ds
from bifrost_research.copilot.harness.policy_schema import (
    EventsLayerPolicy,
    MomentumLayerPolicy,
    SepaLayerPolicy,
)
from bifrost_research.copilot.harness.universe import events as events_mod
from bifrost_research.copilot.harness.universe import momentum as momentum_mod
from bifrost_research.copilot.harness.universe import sepa as sepa_mod


class _FailingCursor:
    def __enter__(self) -> "_FailingCursor":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        raise RuntimeError("operator does not exist: text >= date")

    def fetchall(self) -> list[tuple]:
        return []

    def fetchone(self) -> tuple | None:
        return None

    @property
    def description(self) -> list[tuple[str]]:
        return []


class _RecordingConn:
    """Records whether the caller reset the transaction after a failure."""

    def __init__(self) -> None:
        self.rollbacks = 0

    def cursor(self) -> _FailingCursor:
        return _FailingCursor()

    def rollback(self) -> None:
        self.rollbacks += 1


def _assert_rolled_back(fn: Any) -> None:
    conn = _RecordingConn()
    fn(conn)
    assert conn.rollbacks >= 1, "caught a query error without rolling back"


def test_events_layer_rolls_back() -> None:
    _assert_rolled_back(
        lambda c: events_mod.fetch_event_symbols(c, layer=EventsLayerPolicy())
    )


def test_sepa_layer_rolls_back() -> None:
    _assert_rolled_back(lambda c: sepa_mod.fetch_sepa_symbols(c, layer=SepaLayerPolicy()))


def test_momentum_layer_rolls_back() -> None:
    _assert_rolled_back(
        lambda c: momentum_mod.fetch_momentum_symbols(c, layer=MomentumLayerPolicy())
    )


def test_top_scan_symbols_rolls_back() -> None:
    _assert_rolled_back(lambda c: ds.top_scan_symbols(c, limit=3))


def test_scan_universe_funnel_rolls_back() -> None:
    _assert_rolled_back(lambda c: ds.scan_universe_funnel(c))


def test_failure_still_returns_empty_rather_than_raising() -> None:
    """Rolling back must not change the fail-soft contract itself."""
    conn = _RecordingConn()
    symbols, meta, summary = events_mod.fetch_event_symbols(conn, layer=EventsLayerPolicy())
    assert symbols == []
    assert meta == {}
    assert "failed" in summary
