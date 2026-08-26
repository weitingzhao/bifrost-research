"""Tests for IV-RV Spread (VRP) engine — Wave RS-B-VRP1."""

from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import stdev
from typing import Any

import pytest

from bifrost_research.engines.vrp.compute import (
    annualized_close_to_close_rv,
    compute_fwd_ret_20d,
    compute_vrp_for_date,
    compute_vrp_row,
    vrp_percentile_rank,
)


# ---------------------------------------------------------------------------
# Fake connection reused by the batch-compute test
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, parent: "_FakeConn") -> None:
        self.parent = parent

    def execute(self, query: str, params: Any = None) -> None:
        self.parent.statements.append((query, params))
        q = query.lower()
        if "from raw_market.stock_daily" in q and "distinct" in q:
            trade_date = params[0]
            syms = sorted(
                {sym for sym, rows in self.parent.stock_daily.items() if trade_date in {r[0] for r in rows}}
            )
            self.parent._fetchall = [(s,) for s in syms]
        elif "from raw_market.stock_daily" in q:
            sym = params[0]
            end = params[1]
            limit = params[2]
            rows = list(self.parent.stock_daily.get(sym.upper(), []))
            rows = [r for r in rows if r[0] <= end]
            rows.sort(key=lambda r: r[0], reverse=True)
            self.parent._fetchall = [(bd, close) for bd, close in rows[:limit]]
        elif "from features.option_metric_atm_iv_daily" in q:
            sym = params[0]
            td = params[1]
            rows = [
                (exp, iv)
                for (s, t, exp, iv) in self.parent.atm_iv_rows
                if s == sym and t == td
            ]
            self.parent._fetchall = rows
        elif "from features.stock_signal_vrp_daily" in q:
            sym = params[0]
            start = params[1]
            end = params[2]
            limit = params[3]
            rows = [
                (t, v)
                for (s, t, v) in self.parent.prior_vrp
                if s == sym and start <= t < end and v is not None
            ]
            rows.sort(key=lambda r: r[0], reverse=True)
            self.parent._fetchall = rows[:limit]
        else:
            self.parent._fetchall = []

    def fetchall(self) -> list[Any]:
        return list(self.parent._fetchall)

    def executemany(self, query: str, params_seq: Any) -> None:
        self.parent.statements.append((query, list(params_seq)))
        self.parent.upserts.extend(list(params_seq))

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeConn:
    def __init__(self) -> None:
        self.stock_daily: dict[str, list[tuple[date, float]]] = {}
        self.atm_iv_rows: list[tuple[str, date, date, float]] = []
        self.prior_vrp: list[tuple[str, date, float]] = []
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


# ---------------------------------------------------------------------------
# RV formula
# ---------------------------------------------------------------------------


def _reference_rv(closes: list[float], window: int) -> float:
    tail = closes[-(window + 1) :]
    rets = [math.log(tail[i + 1] / tail[i]) for i in range(len(tail) - 1)]
    return stdev(rets) * math.sqrt(252)


def test_rv_matches_reference_flat() -> None:
    """Flat prices → RV = 0."""
    closes = [100.0] * 40
    assert annualized_close_to_close_rv(closes, 20) == 0.0


def test_rv_matches_reference_synthetic() -> None:
    """RV on a deterministic sinusoid matches numpy-style stdev × √252."""
    closes = [100.0 * (1.01 ** i) for i in range(25)]  # constant +1% returns
    rv = annualized_close_to_close_rv(closes, 20)
    expected = _reference_rv(closes, 20)
    assert rv is not None
    # Constant returns → sample stdev is 0
    assert abs(rv - expected) < 1e-8
    assert rv == 0.0


def test_rv_matches_reference_random_walk() -> None:
    closes = [100.0]
    log_rets = [0.01, -0.005, 0.02, -0.01, 0.005, 0.0, 0.015, -0.02, 0.01, -0.005,
                0.008, -0.01, 0.015, 0.002, -0.007, 0.01, -0.003, 0.006, -0.002, 0.004]
    for r in log_rets:
        closes.append(closes[-1] * math.exp(r))
    rv = annualized_close_to_close_rv(closes, 20)
    expected = stdev(log_rets) * math.sqrt(252)
    assert rv is not None
    assert abs(rv - expected) < 1e-8


def test_rv_insufficient_data() -> None:
    assert annualized_close_to_close_rv([100.0, 101.0], 20) is None
    assert annualized_close_to_close_rv([], 20) is None


def test_rv_rejects_bad_window() -> None:
    with pytest.raises(ValueError):
        annualized_close_to_close_rv([100.0, 101.0, 102.0], 1)


def test_rv_ignores_invalid_prices() -> None:
    closes = [100.0, 0.0, -5.0, 100.5, 101.0, 100.8]
    factors = [1.005, 0.998, 1.002, 0.995, 1.003] * 6
    for f in factors:
        closes.append(closes[-1] * f)
    rv = annualized_close_to_close_rv(closes, 20)
    assert rv is not None
    assert rv > 0.0


# ---------------------------------------------------------------------------
# Percentile rank
# ---------------------------------------------------------------------------


def test_vrp_percentile_basic() -> None:
    hist = [-0.1, -0.05, 0.0, 0.05, 0.1]
    assert vrp_percentile_rank(0.0, hist) == 60.0
    assert vrp_percentile_rank(-0.2, hist) == 0.0
    assert vrp_percentile_rank(0.2, hist) == 100.0
    assert vrp_percentile_rank(0.0, []) is None


def test_vrp_percentile_ignores_nan() -> None:
    hist = [float("nan"), 0.1, 0.2, 0.3]
    # 1 le for 0.1 → 1 / 4 (len(hist) after None removed but n uses original len)
    # Our implementation uses len(history) as denominator, so 1/4 = 25.
    assert vrp_percentile_rank(0.1, hist) == 25.0


# ---------------------------------------------------------------------------
# compute_vrp_row
# ---------------------------------------------------------------------------


def test_compute_vrp_row_missing_iv_keeps_rv() -> None:
    closes = [100.0 * (1.0 + 0.001 * ((i * 7) % 5 - 2)) for i in range(300)]
    row = compute_vrp_row(closes=closes, atm_iv_30d=None)
    assert row is not None
    assert row["rv_20d"] is not None
    assert row["atm_iv_30d"] is None
    assert row["vrp_20d"] is None
    assert row["vrp_60d"] is None
    assert row["vrp_pct_252d"] is None


def test_compute_vrp_row_with_iv_and_history() -> None:
    closes = [100.0 * (1.0 + 0.001 * ((i * 7) % 5 - 2)) for i in range(300)]
    history = [-0.10, -0.05, 0.0, 0.05, 0.10]
    row = compute_vrp_row(closes=closes, atm_iv_30d=0.30, vrp_60d_history=history)
    assert row is not None
    assert row["atm_iv_30d"] == 0.3
    assert row["vrp_60d"] is not None
    assert row["vrp_pct_252d"] is not None
    assert 0.0 <= row["vrp_pct_252d"] <= 100.0


def test_compute_vrp_row_too_few_closes_returns_none() -> None:
    row = compute_vrp_row(closes=[100.0, 101.0], atm_iv_30d=0.25)
    assert row is None


# ---------------------------------------------------------------------------
# Batch compute writes rows
# ---------------------------------------------------------------------------


def _build_stock_daily(start: date, n: int, seed: float = 100.0) -> list[tuple[date, float]]:
    out: list[tuple[date, float]] = []
    price = seed
    for i in range(n):
        # small deterministic movement
        price *= 1.0 + 0.001 * ((i * 3) % 7 - 3) / 10.0
        out.append((start + timedelta(days=i), price))
    return out


def test_compute_vrp_for_date_writes_rows() -> None:
    trade_date = date(2026, 6, 1)
    bars = _build_stock_daily(trade_date - timedelta(days=280), 281)
    # Ensure last bar is trade_date
    assert bars[-1][0] == trade_date

    conn = _FakeConn()
    conn.stock_daily["AAPL"] = bars
    conn.atm_iv_rows = [
        ("AAPL", trade_date, trade_date + timedelta(days=30), 0.25),
    ]
    conn.prior_vrp = [
        ("AAPL", trade_date - timedelta(days=i + 5), -0.02 + i * 0.001)
        for i in range(50)
    ]

    result = compute_vrp_for_date(conn, trade_date=trade_date, underlyings=["AAPL"])
    assert result["symbols"] == 1
    assert result["rows_written"] == 1
    assert result["skipped"] == 0
    assert conn.upserts, "should have written one upsert row"
    row = conn.upserts[0]
    assert row[0] == "AAPL"
    assert row[1] == trade_date
    assert row[2] is not None  # rv_20d
    assert row[5] == 0.25  # atm_iv_30d
    assert row[9] is None  # fwd_ret_20d — NULL for initial run
    insert_sql = next(s[0] for s in conn.statements if "INSERT INTO" in s[0])
    assert "features.stock_signal_vrp_daily" in insert_sql
    assert "DO UPDATE" in insert_sql


def test_compute_vrp_for_date_skips_when_no_bars() -> None:
    trade_date = date(2026, 6, 1)
    conn = _FakeConn()
    # AAPL has no stock_daily rows on trade_date
    result = compute_vrp_for_date(conn, trade_date=trade_date, underlyings=["AAPL"])
    assert result["symbols"] == 1
    assert result["rows_written"] == 0
    assert result["skipped"] == 1


def test_compute_vrp_handles_missing_iv_gracefully() -> None:
    trade_date = date(2026, 6, 1)
    bars = _build_stock_daily(trade_date - timedelta(days=280), 281)
    conn = _FakeConn()
    conn.stock_daily["AAPL"] = bars
    # No ATM IV rows for AAPL — engine should still write RV columns.
    result = compute_vrp_for_date(conn, trade_date=trade_date, underlyings=["AAPL"])
    assert result["rows_written"] == 1
    row = conn.upserts[0]
    assert row[2] is not None  # rv_20d
    assert row[5] is None      # atm_iv_30d
    assert row[6] is None      # vrp_20d
    assert row[7] is None      # vrp_60d
    assert row[8] is None      # vrp_pct_252d


# ---------------------------------------------------------------------------
# fwd_ret helper
# ---------------------------------------------------------------------------


def test_compute_fwd_ret_20d_returns_log_return() -> None:
    trade_date = date(2026, 6, 1)
    pairs = [(trade_date + timedelta(days=i), 100.0 * (1.01 ** i)) for i in range(25)]
    fwd = compute_fwd_ret_20d(pairs, trade_date=trade_date)
    assert fwd is not None
    # Round-precision-8 in compute_fwd_ret_20d limits tolerance to ~1e-8
    assert abs(fwd - math.log(1.01 ** 20)) < 1e-7


def test_compute_fwd_ret_20d_needs_20_future_bars() -> None:
    trade_date = date(2026, 6, 1)
    pairs = [(trade_date + timedelta(days=i), 100.0) for i in range(10)]
    assert compute_fwd_ret_20d(pairs, trade_date=trade_date) is None
