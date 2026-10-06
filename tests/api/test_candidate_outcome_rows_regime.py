"""Candidate outcome rows: source / days filters and the regime label (0.186.0).

Data gaps R6.d (Journal's Settled split needs /rows sliced like /summary) and
R6.b (the Personas bench's Best regime needs a regime on each settled leg).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from bifrost_research.api import candidate_outcome as co


class _Cur:
    def __init__(self, rows: list[tuple[Any, ...]], cols: list[str]) -> None:
        self.rows = rows
        self.description = [(c,) for c in cols]
        self.sql = ""
        self.params: tuple[Any, ...] = ()

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class _Conn:
    def __init__(self, cur: _Cur) -> None:
        self.cur = cur

    def cursor(self) -> _Cur:
        return self.cur


_COLS = [
    "candidate_id", "symbol", "trade_date", "horizon_days", "entry_close", "exit_close",
    "exit_date", "forward_return", "benchmark_symbol", "benchmark_return", "excess_return",
    "hit", "source", "regime", "regime_scope", "regime_date",
]


def _leg(regime: str | None, scope: str | None) -> tuple[Any, ...]:
    d = date(2026, 9, 18)
    return (
        "cand-zzz-1", "ZZZ", d, 1, 10.0, 10.2, date(2026, 9, 21), 0.02, "SPY", 0.01, 0.01,
        True, "harness", regime, scope, d if regime else None,
    )


def test_rows_without_source_or_days_filter_nothing_new() -> None:
    cur = _Cur([], _COLS)
    co.build_rows(_Conn(cur))
    assert cur.params == (100,)
    assert "c.source = %s" not in cur.sql and "CURRENT_DATE" not in cur.sql


def test_rows_take_source_and_days_as_the_summary_does() -> None:
    cur = _Cur([], _COLS)
    co.build_rows(_Conn(cur), symbol=" nvda ", horizon_days=5, source="harness", days=30, limit=7)
    assert "c.trade_date >= CURRENT_DATE - %s::int" in cur.sql and "c.source = %s" in cur.sql
    assert cur.params == (30, "harness", "NVDA", 5, 7)


def test_each_row_carries_its_regime_and_where_it_came_from() -> None:
    cur = _Cur([_leg("trending", "symbol"), _leg("range", "spy"), _leg(None, None)], _COLS)
    rows = co.build_rows(_Conn(cur))
    assert [(r["regime"], r["regime_scope"]) for r in rows] == [
        ("trending", "symbol"),
        ("range", "spy"),
        (None, None),
    ]
    # The name's own terrain first, SPY's when it has none, never older than a week.
    assert "stock_forecast_terrain_daily" in cur.sql and "'SPY'" in cur.sql
    assert f"c.trade_date - {co.REGIME_LOOKBACK_DAYS}" in cur.sql
    assert "ORDER BY t.pref, t.trade_date DESC" in cur.sql


def test_regime_breakdown_keeps_unlabelled_legs_and_reads_hit_rate() -> None:
    cur = _Cur([("trending", 5, 4, 3, 4, 0.02), (None, 5, 2, 0, 0, None)], [])
    got = co.build_regime_breakdown(_Conn(cur), source="harness", days=90)
    assert got[0] == {
        "regime": "trending", "horizon_days": 5, "settled": 4, "judged": 4, "hits": 3,
        "hit_rate": 0.75, "avg_excess": 0.02,
    }
    assert got[1]["regime"] is None and got[1]["hit_rate"] is None
    assert cur.params == (90, "harness")


def test_summary_adds_by_regime_only_when_asked(monkeypatch) -> None:
    calls: list[str] = []

    class _C:
        def close(self) -> None:
            return None

    monkeypatch.setattr(co, "connect", lambda: _C())
    monkeypatch.setattr(co, "build_summary", lambda conn, **kw: {"horizons": []})
    monkeypatch.setattr(
        co, "build_regime_breakdown", lambda conn, **kw: calls.append("regime") or [{"regime": "range"}]
    )
    assert "by_regime" not in co.get_summary(source=None, days=90, by_regime=False)["data"]
    assert calls == []
    got = co.get_summary(source="harness", days=30, by_regime=True)["data"]
    assert got["by_regime"] == [{"regime": "range"}] and calls == ["regime"]
