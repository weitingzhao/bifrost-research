"""Counting the large feature tables off indexes and statistics (0.116.0)."""

from __future__ import annotations

from typing import Any, Self

import pytest

from bifrost_research.db import fastcount


class _Cursor:
    def __init__(self, answers: list[Any]) -> None:
        self.answers = answers
        self.sql: list[str] = []
        self._last: Any = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql.append(" ".join(sql.split()))
        self._last = self.answers.pop(0)

    def fetchone(self) -> Any:
        return self._last

    def fetchall(self) -> Any:
        return self._last


def test_rows_are_the_estimate_when_every_leaf_is_analysed() -> None:
    cur = _Cursor([(2_526_884, 0)])
    assert fastcount.estimate_rows(cur, "features.t") == (2_526_884, True)
    assert len(cur.sql) == 1


def test_rows_are_counted_when_a_leaf_holding_data_was_never_analysed() -> None:
    cur = _Cursor([(10_262, 4), (10_400,)])
    assert fastcount.estimate_rows(cur, "features.t") == (10_400, False)
    assert cur.sql[1] == "SELECT COUNT(*)::bigint FROM features.t"


def test_distinct_values_walk_the_index() -> None:
    cur = _Cursor([(645,)])
    assert fastcount.distinct_count(cur, "features.t", "symbol") == 645
    assert "WITH RECURSIVE walk" in cur.sql[0]
    assert "WHERE symbol > walk.v ORDER BY symbol LIMIT 1" in cur.sql[0]


def test_the_dominant_value_is_the_total_less_the_rest() -> None:
    cur = _Cursor([[("insufficient_inputs", 1_956), ("no_convergence", 35), ("ok", 15_637)]])
    counts = fastcount.breakdown_with_dominant(cur, "features.t", "solver_status", "vendor_snapshot", 2_526_884)
    assert counts["vendor_snapshot"] == 2_526_884 - 1_956 - 35 - 15_637
    # The literal is written in so the planner can match the partial index.
    assert "WHERE solver_status <> 'vendor_snapshot' GROUP BY 1" in cur.sql[0]


def test_a_stale_estimate_never_makes_the_dominant_count_negative() -> None:
    cur = _Cursor([[("insufficient_chain", 50)]])
    counts = fastcount.breakdown_with_dominant(cur, "features.t", "data_quality", "iv_interpolated", 40)
    assert "iv_interpolated" not in counts


@pytest.mark.parametrize("bad", ["features.t; drop", "Features.T", "x y"])
def test_names_that_are_not_plain_identifiers_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        fastcount.distinct_count(_Cursor([]), bad, "symbol")
