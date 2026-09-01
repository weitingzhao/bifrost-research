"""Candidate outcome settlement.

The property that matters most: a horizon that has not elapsed must produce no
row at all. Writing it as a zero return — or as `hit=False` — turns "we do not
know yet" into "this pick lost", and once that reaches a hit-rate average there
is no way to tell the two apart.
"""

from __future__ import annotations

from datetime import date
from typing import Self

from bifrost_research.engines.candidate_outcome import entry as ce
from bifrost_research.engines.candidate_outcome.build import (
    DEFAULT_BENCHMARK,
    DEFAULT_HORIZONS,
    excess_hit,
)

# ------------------ excess_hit ------------------


def test_excess_is_return_minus_benchmark() -> None:
    excess, hit = excess_hit(forward_return=0.0513, benchmark_return=0.0111)
    assert round(excess, 4) == 0.0402
    assert hit is True


def test_beating_the_market_while_falling_still_counts() -> None:
    """Both legs down, but less down than SPY — the funnel added value."""
    _, hit = excess_hit(forward_return=-0.01, benchmark_return=-0.03)
    assert hit is True


def test_rising_less_than_the_market_is_a_miss() -> None:
    _, hit = excess_hit(forward_return=0.02, benchmark_return=0.03)
    assert hit is False


def test_missing_leg_is_unknown_not_a_miss() -> None:
    assert excess_hit(forward_return=None, benchmark_return=0.01) == (None, None)
    assert excess_hit(forward_return=0.01, benchmark_return=None) == (None, None)


# ------------------ outcome_id ------------------


def test_outcome_id_is_stable_and_horizon_scoped() -> None:
    a = ce.outcome_id("cand-1", 5)
    assert a == ce.outcome_id("cand-1", 5)  # re-running settles in place
    assert a != ce.outcome_id("cand-1", 20)
    assert a != ce.outcome_id("cand-2", 5)


# ------------------ build_rows ------------------


class _Cursor:
    """Serves the three query shapes build_rows issues, keyed on SQL text."""

    def __init__(self, world: _World) -> None:
        self._w = world
        self._rows: list[tuple] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        if "candidate_pool" in sql:
            self._rows = self._w.pending
        elif "bar_date >= %s" in sql:
            symbol, as_of, limit = params
            bars = self._w.bars.get(symbol, [])
            self._rows = [(d, c) for d, c in bars if d >= as_of][:limit]
        elif "bar_date = %s" in sql:
            symbol, day = params
            self._rows = [(c,) for d, c in self._w.bars.get(symbol, []) if d == day]
        else:  # pragma: no cover
            self._rows = []

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _World:
    def __init__(self, pending: list[tuple], bars: dict[str, list[tuple[date, float]]]) -> None:
        self.pending = pending
        self.bars = bars

    def cursor(self) -> _Cursor:
        return _Cursor(self)


_SESSIONS = [date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19), date(2026, 8, 20)]


def _bars(prices: list[float]) -> list[tuple[date, float]]:
    return list(zip(_SESSIONS, prices))


def test_settles_an_elapsed_horizon_against_the_benchmark() -> None:
    world = _World(
        pending=[("cand-1", "NVDA", date(2026, 8, 17), [])],
        bars={
            "NVDA": _bars([100.0, 101.0, 102.0, 110.0]),
            "SPY": _bars([200.0, 201.0, 202.0, 204.0]),
        },
    )
    rows, stats = ce.build_rows(world, horizons=(3,))
    assert stats["settled"] == 1
    (row,) = rows
    cols = dict(zip(ce.UPSERT_COLS, row))
    assert cols["symbol"] == "NVDA"
    assert cols["horizon_days"] == 3
    assert cols["entry_close"] == 100.0
    assert cols["exit_close"] == 110.0
    assert cols["exit_date"] == date(2026, 8, 20)
    assert round(cols["forward_return"], 4) == 0.10
    assert round(cols["benchmark_return"], 4) == 0.02
    assert round(cols["excess_return"], 4) == 0.08
    assert cols["hit"] is True


def test_unelapsed_horizon_writes_no_row() -> None:
    world = _World(
        pending=[("cand-1", "NVDA", date(2026, 8, 17), [])],
        bars={"NVDA": _bars([100.0, 101.0, 102.0, 110.0]), "SPY": _bars([200.0] * 4)},
    )
    rows, stats = ce.build_rows(world, horizons=(20,))
    assert rows == []
    assert stats["settled"] == 0
    assert stats["not_elapsed"] == 1


def test_already_settled_horizons_are_skipped() -> None:
    world = _World(
        pending=[("cand-1", "NVDA", date(2026, 8, 17), [1, 3])],
        bars={"NVDA": _bars([100.0, 101.0, 102.0, 110.0]), "SPY": _bars([200.0] * 4)},
    )
    rows, stats = ce.build_rows(world, horizons=(1, 3))
    assert rows == []
    assert stats["candidates"] == 0


def test_missing_benchmark_leaves_hit_unknown_but_keeps_the_return() -> None:
    """A settled price with no benchmark is still worth recording."""
    world = _World(
        pending=[("cand-1", "NVDA", date(2026, 8, 17), [])],
        bars={"NVDA": _bars([100.0, 101.0, 102.0, 110.0])},  # no SPY at all
    )
    rows, stats = ce.build_rows(world, horizons=(3,))
    cols = dict(zip(ce.UPSERT_COLS, rows[0]))
    assert round(cols["forward_return"], 4) == 0.10
    assert cols["benchmark_return"] is None
    assert cols["benchmark_symbol"] is None
    assert cols["hit"] is None
    assert stats["no_benchmark"] == 1


def test_defaults_cover_short_and_long_horizons() -> None:
    assert DEFAULT_HORIZONS == (1, 5, 20)
    assert DEFAULT_BENCHMARK == "SPY"


def test_upsert_keys_match_the_unique_constraint() -> None:
    """conflict_keys must mirror UNIQUE (candidate_id, horizon_days)."""
    assert "candidate_id" in ce.UPSERT_COLS
    assert "horizon_days" in ce.UPSERT_COLS
    # Identity columns must never be in the update set or a re-settle rewrites them.
    for col in ("id", "candidate_id", "symbol", "trade_date", "horizon_days"):
        assert col not in ce.UPDATE_COLS

