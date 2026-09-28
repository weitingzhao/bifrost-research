"""IV coverage heal: which sessions are short, and the order they are recomputed in."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from bifrost_research.engines.volatility import iv_coverage_heal as heal

EARLY = datetime(2026, 8, 20, tzinfo=timezone.utc)
LATE = datetime(2026, 9, 28, tzinfo=timezone.utc)
D = [date(2026, 8, d) for d in (24, 25, 26, 27, 28)]


def test_months_clip_to_the_range() -> None:
    assert list(heal.months(date(2026, 7, 15), date(2026, 9, 3))) == [
        (date(2026, 7, 15), date(2026, 7, 31)),
        (date(2026, 8, 1), date(2026, 8, 31)),
        (date(2026, 9, 1), date(2026, 9, 3)),
    ]


def test_a_session_short_of_raw_written_since_is_short() -> None:
    """2026-08-26: 315 names with ATM IV against 652 in raw, refilled after."""
    raw = {d: (652, LATE) for d in D}
    feat = {d: (600, EARLY) for d in D} | {D[2]: (315, EARLY)}
    assert heal.short_sessions(D, raw, feat) == [(D[2], 315, 652)]


def test_ATM_filters_dropping_a_few_names_is_not_short() -> None:
    """2025-03-12 recomputed: 546 of 588 (0.93)."""
    assert heal.short_sessions(D[:1], {D[0]: (588, LATE)}, {D[0]: (546, EARLY)}) == []


def test_a_short_session_recomputed_since_raw_was_written_is_left_alone() -> None:
    """What raw supports once recomputed; rerunning it weekly reruns every session after it."""
    assert heal.short_sessions(D[:1], {D[0]: (652, EARLY)}, {D[0]: (315, LATE)}) == []


def test_a_session_with_no_features_is_short_and_one_with_no_raw_is_not() -> None:
    raw = {D[0]: (652, EARLY)}
    assert heal.short_sessions(D[:2], raw, {}) == [(D[0], 0, 652)]


def test_raw_is_read_a_month_at_a_time_with_literal_bounds() -> None:
    seen: list[tuple[str, Any]] = []

    class _Cur:
        def __enter__(self) -> _Cur:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            seen.append((sql, params))

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [(D[0], 3, LATE)] if "option_daily" in seen[-1][0] else []

    class _Conn:
        def cursor(self) -> _Cur:
            return _Cur()

        def commit(self) -> None:
            return None

    out = heal.raw_breadth(_Conn(), ["SPY"], date(2026, 7, 15), date(2026, 8, 3))
    reads = [s for s, _p in seen if "option_daily" in s]
    assert len(reads) == 2
    assert "DATE '2026-07-15'" in reads[0] and "DATE '2026-07-31'" in reads[0]
    assert "DATE '2026-08-01'" in reads[1] and "DATE '2026-08-03'" in reads[1]
    assert out == {D[0]: (3, LATE)}


def test_heal_recomputes_atm_on_short_sessions_and_ranks_every_session_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, date]] = []

    def rec(name: str):
        def fn(conn: Any, *, trade_date: date, underlyings: Any, **kw: Any) -> dict[str, int]:
            calls.append((name, trade_date))
            return {"rows_written": 1}

        return fn

    for name, attr in (
        ("atm", "compute_atm_iv_for_date"),
        ("pcr", "compute_pcr_for_date"),
        ("pct", "compute_iv_percentile_for_date"),
        ("vrp", "compute_vrp_for_date"),
    ):
        monkeypatch.setattr(heal, attr, rec(name))

    class _Conn:
        def commit(self) -> None:
            return None

    out = heal.heal(_Conn(), [D[3], D[1]], D, ["SPY"])
    assert [c for c in calls if c[0] == "atm"] == [("atm", D[1]), ("atm", D[3])]
    assert [d for n, d in calls if n == "pct"] == D[1:]
    assert [d for n, d in calls if n == "vrp"] == D[1:]
    assert calls.index(("atm", D[3])) < calls.index(("pct", D[3]))
    assert out["sessions"] == 4 and out["atm_rows"] == 2


def test_heal_with_nothing_short_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heal, "compute_iv_percentile_for_date", lambda *a, **k: pytest.fail("ran"))
    assert heal.heal(object(), [], D, ["SPY"])["sessions"] == 0
