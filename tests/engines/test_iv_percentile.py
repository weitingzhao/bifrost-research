"""Tests for IV Percentile / Rank."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from bifrost_research.engines.volatility.iv_percentile import (
    compute_iv_percentile_for_date,
    iv_percentile,
    iv_rank,
    rollup_daily_iv_by_symbol,
)


class _FakeCursor:
    def __init__(self, parent: _FakeConn) -> None:
        self.parent = parent

    def execute(self, query: str, params: Any = None) -> None:
        self.parent.statements.append((query, params))
        q = query.lower()
        if "from features.option_metric_atm_iv_daily" in q:
            from_d = params[0] if params else None
            to_d = params[1] if params and len(params) > 1 else None
            underlyings = set(params[2]) if params and len(params) > 2 else None
            rows = []
            for r in self.parent.atm_rows:
                td = r["trade_date"]
                if from_d is not None and td < from_d:
                    continue
                if to_d is not None and td > to_d:
                    continue
                if underlyings is not None and r["symbol"] not in underlyings:
                    continue
                rows.append((r["symbol"], r["trade_date"], r["expiry"], r["atm_iv"]))
            self.parent._fetchall = rows
        else:
            self.parent._fetchall = []

    def fetchall(self) -> list[Any]:
        return list(self.parent._fetchall)

    def executemany(self, query: str, params_seq: Any) -> None:
        self.parent.statements.append((query, list(params_seq)))
        self.parent.upserts.extend(list(params_seq))

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeConn:
    def __init__(self, atm_rows: list[dict[str, Any]] | None = None) -> None:
        self.atm_rows = atm_rows or []
        self.statements: list[tuple[str, Any]] = []
        self.upserts: list[tuple[Any, ...]] = []
        self._fetchall: list[Any] = []
        self.committed = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        return None


def test_iv_percentile_synthetic() -> None:
    # History: 0.10, 0.20, 0.30, 0.40, 0.50 — current 0.30 → 3/5 = 60
    hist = [0.10, 0.20, 0.30, 0.40, 0.50]
    assert iv_percentile(0.30, hist) == 60.0
    assert iv_percentile(0.05, hist) == 0.0
    assert iv_percentile(0.50, hist) == 100.0
    assert iv_percentile(0.30, []) is None


def test_iv_rank_synthetic() -> None:
    hist = [0.10, 0.20, 0.30, 0.40, 0.50]
    assert iv_rank(0.30, hist) == 50.0
    assert iv_rank(0.10, hist) == 0.0
    assert iv_rank(0.50, hist) == 100.0
    assert iv_rank(0.25, [0.25, 0.25, 0.25]) == 50.0
    assert iv_rank(0.1, []) is None


def test_rollup_is_iv30_not_the_median_across_expiries() -> None:
    """PLTR 2026-09-22: 19 expiries, 13 past 60 DTE; the median read 0.564, IV30 0.464."""
    td = date(2024, 6, 20)
    by_sym = rollup_daily_iv_by_symbol(
        [
            {"symbol": "AAPL", "trade_date": td, "expiry": td + timedelta(days=21), "atm_iv": 0.20},
            {"symbol": "AAPL", "trade_date": td, "expiry": td + timedelta(days=49), "atm_iv": 0.34},
            {"symbol": "AAPL", "trade_date": td, "expiry": td + timedelta(days=365), "atm_iv": 0.60},
            {"symbol": "AAPL", "trade_date": td, "expiry": td + timedelta(days=730), "atm_iv": 0.60},
        ]
    )
    assert by_sym["AAPL"][td] == pytest.approx(0.20 + 0.14 * 9 / 28)


def test_compute_iv_percentile_upsert() -> None:
    """Five days of synthetic ATM IV → known percentile/rank on last day."""
    base = date(2024, 6, 14)
    expiry = base + timedelta(days=34)
    # daily IV: 0.10, 0.20, 0.30, 0.40, 0.50 on consecutive weekdays
    atm_rows = []
    for i, iv in enumerate([0.10, 0.20, 0.30, 0.40, 0.50]):
        atm_rows.append(
            {
                "symbol": "AAPL",
                "trade_date": base + timedelta(days=i),
                "expiry": expiry,
                "atm_iv": iv,
            }
        )
    td = base + timedelta(days=4)  # 0.50 current
    conn = _FakeConn(atm_rows)
    result = compute_iv_percentile_for_date(
        conn,
        trade_date=td,
        underlyings=["AAPL"],
        percentile_window=5,
    )
    assert result["groups"] == 1
    row = conn.upserts[0]
    assert row[0] == "AAPL"
    assert row[2] == 0.50  # iv_current
    assert row[3] == 100.0  # percentile
    assert row[4] == 100.0  # rank
    assert row[5] == 5  # lookback_days
    insert_sql = next(s[0] for s in conn.statements if "INSERT INTO" in s[0])
    assert "features.option_metric_iv_percentile_daily" in insert_sql
    assert "DO UPDATE" in insert_sql


def test_compute_iv_percentile_empty() -> None:
    conn = _FakeConn([])
    result = compute_iv_percentile_for_date(conn, trade_date=date(2024, 6, 20))
    assert result["groups"] == 0
    assert conn.upserts == []


def test_percentile_and_rank_stay_null_until_the_window_holds_126_sessions() -> None:
    """AMD had 11 sessions on 2026-09-23 and still reported iv_rank_1y."""
    base = date(2026, 9, 8)
    rows = [
        {"symbol": "AMD", "trade_date": base + timedelta(days=i), "expiry": base + timedelta(days=40), "atm_iv": 0.5 + i / 100}
        for i in range(11)
    ]
    td = base + timedelta(days=10)
    conn = _FakeConn(rows)
    result = compute_iv_percentile_for_date(conn, trade_date=td, underlyings=["AMD"])
    assert result["rows_written"] == 1
    (row,) = conn.upserts
    assert row[2] == pytest.approx(0.60)  # iv_current is still written
    assert row[3] is None and row[4] is None
    assert row[5] == 11


def test_history_query_keeps_only_expiries_iv30_can_use() -> None:
    conn = _FakeConn([])
    compute_iv_percentile_for_date(conn, trade_date=date(2026, 9, 22), underlyings=["PLTR"])
    sql = next(q for q, _ in conn.statements if "option_metric_atm_iv_daily" in q)
    assert "BETWEEN 7 AND 90" in sql
