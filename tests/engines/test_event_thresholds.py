"""SEPA and IV percentile thresholds are on a 0–100 scale (0.176.1)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from bifrost_research.engines.backtest.event_defs import EventDef
from bifrost_research.engines.backtest.event_query import resolve_events_between


class _Conn:
    def __init__(self) -> None:
        self.params: tuple[Any, ...] | None = None

    def cursor(self) -> "_Conn":
        return self

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def rollback(self) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.params = params

    def fetchall(self) -> list[tuple]:
        return []


S, E = date(2026, 1, 1), date(2026, 6, 1)


@pytest.mark.parametrize(("kind", "default"), [("sepa_hit", 70.0), ("iv_percentile_threshold", 80.0)])
def test_the_default_threshold_is_on_the_scores_scale(kind: str, default: float) -> None:
    conn = _Conn()
    resolve_events_between(conn, EventDef(kind=kind, params={}), S, E)  # type: ignore[arg-type]
    assert conn.params is not None and conn.params[2] == default


@pytest.mark.parametrize("kind", ["sepa_hit", "iv_percentile_threshold"])
@pytest.mark.parametrize("bad", [0.7, 0.8, 1, 0, -5, 101])
def test_a_fraction_or_an_out_of_range_threshold_is_refused(kind: str, bad: float) -> None:
    with pytest.raises(ValueError, match="0–100 scale"):
        resolve_events_between(_Conn(), EventDef(kind=kind, params={"threshold": bad}), S, E)  # type: ignore[arg-type]


def test_an_explicit_threshold_is_used_as_given() -> None:
    conn = _Conn()
    resolve_events_between(conn, EventDef(kind="iv_percentile_threshold", params={"threshold": 20, "direction": "below"}), S, E)
    assert conn.params is not None and conn.params[2] == 20.0
