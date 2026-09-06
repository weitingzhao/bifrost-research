"""A3 — similar-regime neighbours are resolved, de-clustered and summarised."""

from __future__ import annotations

from datetime import date
from typing import Any

from bifrost_research.api.similar_regime import (
    OVERFETCH,
    neighbour_hygiene,
    similar_report,
)


def _row(d: str, distance: float, fwd: float | None) -> dict[str, Any]:
    return {"trade_date": d, "distance": distance, "fwd_return": fwd}


def test_hygiene_drops_unresolved_keeps_nearest_of_a_cluster_and_cuts_to_k() -> None:
    rows = [
        _row("2026-03-19", 0.7, 0.076),   # nearest — kept
        _row("2026-03-20", 5.0, 0.093),   # one session later — same episode, dropped
        _row("2026-06-25", 13.6, 0.021),  # kept
        _row("2026-08-31", 1.8, None),    # unresolved — dropped
        _row("2026-07-08", 14.0, -0.010), # kept
        _row("2026-07-10", 14.5, 0.000),  # within a week of 07-08 — dropped
        _row("2026-01-05", 20.0, 0.030),  # would be fourth — beyond k
    ]
    kept, hygiene = neighbour_hygiene(rows, k=3)
    assert [r["trade_date"] for r in kept] == ["2026-03-19", "2026-06-25", "2026-07-08"]
    # The walk stops once k neighbours are kept, so 07-10 is never examined; the
    # accounting reports what was dropped while filling k, not the whole tail.
    assert hygiene == {"fetched": 7, "dropped_unresolved": 1, "dropped_clustered": 1, "min_gap_days": 7}
    assert all(r["fwd_return"] is not None for r in kept)


class _Cur:
    def __init__(self, rows: list[tuple[Any, ...]], bars: list[tuple[Any, ...]]) -> None:
        self._rows, self._bars = rows, bars
        self._out: list[tuple[Any, ...]] = []
        self.description: list[tuple[str]] = []
        self.limit: int | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        if "stock_signal_vrp_daily" in sql:
            self.limit = int(params[-1])
            self._out = self._rows[: self.limit]
            self.description = [(c,) for c in ("trade_date", "symbol", "vrp_pct_252d", "vrp_60d", "atm_iv_30d", "rv_60d")]
        else:
            start = params[1]
            self._out = [b for b in self._bars if b[0] >= start][: int(params[2])]
            self.description = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._out)

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, rows: list[tuple[Any, ...]], bars: list[tuple[Any, ...]]) -> None:
        self.rows, self.bars = rows, bars
        self.cursors: list[_Cur] = []

    def cursor(self) -> _Cur:
        cur = _Cur(self.rows, self.bars)
        self.cursors.append(cur)
        return cur


def test_similar_report_overfetches_then_returns_k_resolved_neighbours_with_a_summary() -> None:
    rows = [
        (date(2026, 3, 19), "SPY", 85.7, 1.2, 1.36, 0.12),
        (date(2026, 3, 20), "SPY", 80.0, 1.1, 1.20, 0.12),
        (date(2026, 6, 25), "SPY", 71.4, 0.5, 0.60, 0.10),
        (date(2026, 9, 3), "SPY", 84.0, 0.9, 0.95, 0.11),  # too recent to have a forward return
    ]
    bars = [(date(2026, 3, 19) if i == 0 else date(2026, 1, 1), 100.0 + i) for i in range(1)]
    # six bars from each resolved date: +1 per session so the 5-session return is +5%
    bars = []
    for start in (date(2026, 3, 19), date(2026, 3, 20), date(2026, 6, 25)):
        for i in range(6):
            bars.append((date.fromordinal(start.toordinal() + i), 100.0 + i))
    conn = _Conn(rows, bars)
    out_rows, source, used, extra = similar_report(conn, lens="vrp", symbol="spy", value="85", k=2, horizon=5)
    assert conn.cursors[0].limit == 2 * OVERFETCH
    assert used == 85.0 and "vrp" in source
    assert [r["trade_date"] for r in out_rows] == ["2026-03-19", "2026-06-25"]
    assert extra["hygiene"]["dropped_unresolved"] == 1 and extra["hygiene"]["dropped_clustered"] == 1
    assert extra["summary"]["n_resolved"] == 2 and abs(extra["summary"]["median_fwd"] - 0.05) < 1e-9
