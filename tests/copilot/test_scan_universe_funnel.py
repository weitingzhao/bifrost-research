"""Scan universe funnel — counts must describe the day, not the LIMITed result.

`top_scan_symbols` queries with LIMIT, so a funnel built from its rows reports
`3 -> 3` however large the universe was.  These lock the separate whole-day
count and the zero-input floor that keeps neutral-50 rows out of the ranking.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.copilot.harness import data_sources as ds
from bifrost_research.copilot.harness.policy_schema import LoopPolicy
from bifrost_research.copilot.harness.universe.scan_legacy import resolve_scan_legacy


class _Cursor:
    def __init__(self, row: tuple | None) -> None:
        self._row = row
        self.executed: list[tuple[str, tuple]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed.append((sql, tuple(params)))

    def fetchone(self) -> tuple | None:
        return self._row

    def fetchall(self) -> list[tuple]:
        return [self._row] if self._row else []

    @property
    def description(self) -> list[tuple[str]]:
        return []


class _Conn:
    def __init__(self, row: tuple | None) -> None:
        self.cur = _Cursor(row)

    def cursor(self) -> _Cursor:
        return self.cur


class _RaisingConn:
    def cursor(self) -> Any:
        raise RuntimeError("db unreachable")


# ------------------ scan_universe_funnel ------------------


def test_funnel_reports_each_stage() -> None:
    conn = _Conn(("2026-08-31", 26, 25, 25, 3))
    out = ds.scan_universe_funnel(conn, min_composite_score=40.0)
    assert out["total"] == 26
    assert out["with_inputs"] == 25
    assert out["score_passed"] == 3
    # The whole-day count is the point: it must exceed what a LIMIT would return.
    assert out["total"] > out["score_passed"]


def test_funnel_counts_the_day_not_the_limit() -> None:
    """No LIMIT may appear — that is the bug this function exists to avoid."""
    conn = _Conn(("2026-08-31", 26, 25, 25, 25))
    ds.scan_universe_funnel(conn)
    sql, _ = conn.cur.executed[0]
    assert "LIMIT" not in sql.upper()


def test_funnel_fails_soft() -> None:
    out = ds.scan_universe_funnel(_RaisingConn())
    assert out == {
        "trade_date": None,
        "total": 0,
        "with_inputs": 0,
        "flag_passed": 0,
        "score_passed": 0,
    }


def test_funnel_binds_flag_params_twice() -> None:
    """flag_pred appears in two FILTER clauses, so its params are supplied twice."""
    conn = _Conn(("2026-08-31", 26, 25, 4, 4))
    ds.scan_universe_funnel(conn, flag_filter="iv_rank:hot", min_composite_score=50.0)
    _, params = conn.cur.executed[0]
    assert params == ("iv_rank", "hot", "iv_rank", "hot", 50.0)


# ------------------ zero-input floor ------------------


def test_top_scan_symbols_excludes_zero_input_rows() -> None:
    conn = _Conn(None)
    ds.top_scan_symbols(conn, limit=3)
    sql, _ = conn.cur.executed[0]
    # Every scoring input must be named in the predicate; a row with none of them
    # carries the neutral 50 and would otherwise outrank real scores.
    for col in ds._SCAN_INPUT_COLUMNS:
        assert f"({col} IS NOT NULL)::int" in sql
    assert ">= 1" in sql


def test_top_scan_symbols_floor_can_be_disabled() -> None:
    conn = _Conn(None)
    ds.top_scan_symbols(conn, limit=3, min_inputs=0)
    sql, _ = conn.cur.executed[0]
    assert "IS NOT NULL)::int" not in sql


def test_close_is_not_a_scoring_input() -> None:
    """Gating on close would empty 20 of 61 measured trading days rather than 2."""
    assert "close" not in ds._SCAN_INPUT_COLUMNS


# ------------------ resolve_scan_legacy ------------------


def _policy(**kw: Any) -> LoopPolicy:
    return LoopPolicy(universe_mode="scan_legacy", **kw)


def test_scan_legacy_funnel_spans_universe_to_proposed(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        ds,
        "scan_universe_funnel",
        lambda conn, **kw: {
            "trade_date": "2026-08-31",
            "total": 26,
            "with_inputs": 25,
            "flag_passed": 25,
            "score_passed": 25,
        },
    )
    monkeypatch.setattr(
        ds,
        "top_scan_symbols",
        lambda conn, **kw: [{"symbol": s} for s in ("NNE", "MRVL", "PLTR")],
    )

    result = resolve_scan_legacy(_Conn(None), _policy(max_candidates=3), limit=3)

    assert result.symbols == ["NNE", "MRVL", "PLTR"]
    steps = {s.name: s for s in result.funnel}
    assert steps["scan_universe"].in_count == 26
    assert steps["scan_universe"].out_count == 25
    assert steps["top_n"].out_count == 3
    # The regression this guards: a funnel whose first step reads 3 -> 3.
    assert steps["scan_universe"].in_count > steps["top_n"].out_count


def test_scan_legacy_marks_unset_filters_optional(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        ds,
        "scan_universe_funnel",
        lambda conn, **kw: {
            "trade_date": "2026-08-31",
            "total": 26,
            "with_inputs": 25,
            "flag_passed": 25,
            "score_passed": 25,
        },
    )
    monkeypatch.setattr(ds, "top_scan_symbols", lambda conn, **kw: [{"symbol": "NNE"}])

    result = resolve_scan_legacy(_Conn(None), _policy(), limit=1)

    steps = {s.name: s for s in result.funnel}
    assert steps["flag_filter"].optional is True
    assert steps["min_composite_score"].optional is True
