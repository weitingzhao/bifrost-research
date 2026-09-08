"""A candidate proposed on a non-trading day must still be judgeable.

`_forward_leg` takes bars at or after `trade_date`, so a Sunday candidate
enters on Monday. The benchmark leg used the exact `trade_date`, where SPY has
no bar — so `benchmark_return` came back None, `hit` was written NULL, and the
row was never revisited. Eleven of thirty harness candidates were proposed on
a Sunday; the leash needs five judged outcomes and was being fed empty rows.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Self

from bifrost_research.engines.candidate_outcome import entry as co


class _Cur:
    """Bars for one symbol keyed by date, answering both query shapes."""

    def __init__(self, bars: dict[str, dict[date, float]]) -> None:
        self.bars = bars
        self._rows: list[tuple] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> None:
        symbol = str(params[0])
        series = sorted(self.bars.get(symbol, {}).items())
        if "bar_date >= %s" in sql:  # _forward_leg
            as_of, limit = params[1], params[2]
            self._rows = [(d, c) for d, c in series if d >= as_of][:limit]
        else:  # _close_on
            want = params[1]
            self._rows = [(c,) for d, c in series if d == want]

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, bars: dict[str, dict[date, float]]) -> None:
        self.bars = bars

    def cursor(self) -> _Cur:
        return _Cur(self.bars)


# Friday, then the following Monday and Tuesday. No weekend bars, for anyone.
FRI, MON, TUE = date(2026, 9, 4), date(2026, 9, 7), date(2026, 9, 8)
SUNDAY = date(2026, 9, 6)

BARS: dict[str, dict[date, float]] = {
    "HALO": {FRI: 100.0, MON: 110.0, TUE: 121.0},
    "SPY": {FRI: 500.0, MON: 505.0, TUE: 510.0},
}


def _rows(trade_date: date) -> list[tuple]:
    conn = _Conn(BARS)
    cand = {"id": "cand-halo-x", "symbol": "HALO", "trade_date": trade_date, "horizons": [1]}
    original = co._pending
    co._pending = lambda *a, **k: [cand]  # type: ignore[assignment]
    try:
        rows, _stats = co.build_rows(conn, horizons=[1])
    finally:
        co._pending = original  # type: ignore[assignment]
    return rows


def _row_to_fields(row: tuple) -> dict[str, Any]:
    """Column order per the INSERT in `build_rows`."""
    return {"benchmark_return": row[10], "excess": row[11], "hit": row[12], "exit_date": row[7]}


def test_a_sunday_candidate_is_judged_against_the_same_window() -> None:
    got = _row_to_fields(_rows(SUNDAY)[0])
    # Enters Monday (the first bar at or after Sunday), exits Tuesday.
    assert got["exit_date"] == TUE
    # SPY priced on those same two days: 505 -> 510.
    assert got["benchmark_return"] is not None
    assert got["hit"] is True, "HALO +10% vs SPY +1% is a hit, not an unjudgeable row"


def test_a_trading_day_candidate_is_unchanged() -> None:
    got = _row_to_fields(_rows(FRI)[0])
    assert got["exit_date"] == MON
    assert got["benchmark_return"] is not None and got["hit"] is True


def test_pending_returns_a_horizon_whose_row_has_no_verdict():
    """A settled row without a verdict must come back, not count as done.

    ``excess_hit`` writes ``(None, None)`` when a leg is missing — correct, an
    unknown outcome is not a miss. But ``_pending`` used to treat any existing
    row as settled, so that blank was permanent even though the write is an
    upsert that would have overwritten it. The leash reads this table for the
    source track record; a blank there is evidence that can never be judged.
    """
    from bifrost_research.engines.candidate_outcome.entry import _pending

    captured: dict[str, object] = {}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            captured["sql"] = sql

        def fetchall(self):
            # One candidate: horizon 1 has a verdict, horizon 5 is blank.
            return [("cand-1", "AAPL", None, [1])]

    class _Conn:
        def cursor(self):
            return _Cur()

    out = _pending(_Conn(), lookback_days=90, horizons=(1, 5))

    sql = str(captured["sql"])
    assert "o.hit IS NOT NULL" in sql, "a row without a hit must not count as settled"
    assert "o.benchmark_return IS NOT NULL" in sql, "nor one without a benchmark"
    assert len(out) == 1
    assert out[0]["horizons"] == [5], "only the unjudged horizon should come back"
