"""Unit tests for Wave D scanner build helpers."""

from __future__ import annotations

from datetime import date

from bifrost_research.engines.scan.build import (
    build_lens_flags,
    build_scan_row,
    compute_composite,
    flag_for_score,
)


def test_flag_for_score_hot_cold_neutral() -> None:
    assert flag_for_score(85) == "hot"
    assert flag_for_score(80) == "hot"
    assert flag_for_score(15) == "cold"
    assert flag_for_score(20) == "cold"
    assert flag_for_score(50) == "neutral"
    assert flag_for_score(40) == "neutral"
    assert flag_for_score(60) == "neutral"
    assert flag_for_score(70) is None
    assert flag_for_score(None) is None


def test_compute_composite_weighted_average() -> None:
    parts = {
        "iv_rank_1y": 80.0,
        "vrp_pct_252d": 60.0,
        "atm_slope_30d": 0.0,
        "pin_pct_distance": 0.0,
        "pin_score": 70.0,
    }
    score = compute_composite(parts)
    assert score is not None
    assert 60.0 <= score <= 75.0


def test_compute_composite_uses_default_terrain_when_missing() -> None:
    parts = {
        "iv_rank_1y": 100.0,
        "vrp_pct_252d": 100.0,
    }
    score = compute_composite(parts)
    assert score is not None
    assert score > 80.0


def test_build_lens_flags_sparse() -> None:
    flags = build_lens_flags(
        iv_rank_1y=90.0,
        vrp_pct_252d=10.0,
        atm_slope_30d=0.0,
        pin_pct_distance=0.0,
        pin_score=75.0,
    )
    assert flags["iv_rank"] == "hot"
    assert flags["vrp"] == "cold"
    assert flags["atm_slope"] == "neutral"
    assert flags["pin"] == "neutral"
    assert "terrain" not in flags


def test_build_scan_row_shape() -> None:
    row = build_scan_row(
        trade_date=date(2026, 8, 22),
        symbol="nvda",
        iv_rank_1y=88.0,
        vrp_pct_252d=12.0,
        pin_score=62.0,
    )
    assert row["symbol"] == "NVDA"
    assert row["trade_date"] == date(2026, 8, 22)
    assert row["composite_score"] is not None
    assert isinstance(row["lens_flags"], dict)
    assert row["lens_flags"]["iv_rank"] == "hot"


# ── the symbols_filter had never been used ────────────────────────────────


class _FilterCur:
    """Records the statement instead of running it."""

    description = [("symbol",)]

    def __init__(self) -> None:
        self.sql = ""
        self.params: object = None

    def execute(self, sql: str, params: object = None) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self) -> list[tuple[object, ...]]:
        return []

    def __enter__(self) -> _FilterCur:
        return self

    def __exit__(self, *a: object) -> None:
        return None


class _FilterConn:
    def __init__(self) -> None:
        self.cur = _FilterCur()

    def cursor(self) -> _FilterCur:
        return self.cur


def test_a_symbols_filter_lands_before_the_order_by() -> None:
    """A WHERE after an ORDER BY is a syntax error, and this argument had no
    caller until a scoped recompute passed it — so it had never run at all."""
    from bifrost_research.engines.scan.entry import fetch_scan_source_rows

    conn = _FilterConn()
    fetch_scan_source_rows(conn, date(2026, 6, 24), ["ECHO"], symbols_filter=["ECHO"])
    sql = conn.cur.sql
    assert "WHERE u.symbol = ANY(%s)" in sql
    assert sql.index("WHERE u.symbol = ANY(%s)") < sql.index("ORDER BY u.symbol")
    assert sql.rstrip().endswith("ORDER BY u.symbol")


def test_without_a_filter_the_statement_still_orders() -> None:
    from bifrost_research.engines.scan.entry import fetch_scan_source_rows

    conn = _FilterConn()
    fetch_scan_source_rows(conn, date(2026, 6, 24), ["ECHO"])
    assert "WHERE u.symbol = ANY(%s)" not in conn.cur.sql
    assert conn.cur.sql.rstrip().endswith("ORDER BY u.symbol")
