"""``create_sim_run`` stores run, trades and curve in one transaction."""

from __future__ import annotations

import json
from typing import Any

import pytest

from bifrost_research.repositories import backtest_run as repo


class _Cur:
    def __init__(self, log: list[tuple[str, Any]], fail_on: str | None) -> None:
        self.log, self.fail_on = log, fail_on

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.log.append(("execute", sql, params))

    def executemany(self, sql: str, rows: list[Any]) -> None:
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("relation does not exist")
        self.log.append(("executemany", sql, rows))

    def fetchone(self) -> dict[str, Any]:
        return {"id": "bt_x", "summary": {}, "event_def": {}, "fill_config": {}}


class _Conn:
    def __init__(self, fail_on: str | None = None) -> None:
        self.log: list[Any] = []
        self.fail_on = fail_on
        self.commits = self.rollbacks = 0

    def cursor(self) -> _Cur:
        return _Cur(self.log, self.fail_on)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


_TRADE = {
    "seq": 1, "symbol": "NVDA", "structure": "short_put", "entry_date": "2025-01-02",
    "exit_date": "2025-01-20", "exit_reason": "profit_take", "legs": [{"ticker": "O:NVDA"}],
    "entry_credit": 120.0, "exit_debit": -60.0, "pnl": 58.7, "max_loss": None, "margin": 2400.0,
    "days_held": 18, "mfe": 60.0, "mae": -10.0, "fill_basis": "vwap+tiered_slippage",
}
_EQ = {"as_of": "2025-01-02", "equity": 100000.0, "margin_used": 2400.0, "open_positions": 1}


def _store(conn: _Conn, **kw: Any) -> dict[str, Any]:
    return repo.create_sim_run(
        conn, params={"structure": "short_put"}, summary={"n_trades": 1},
        trades=[_TRADE], equity=[_EQ], lookback_years=1, **kw,
    )


def test_run_trades_and_curve_commit_together() -> None:
    conn = _Conn()
    out = _store(conn)
    assert out["engine"] == "sim"
    run_sql, run_params = conn.log[0][1], conn.log[0][2]
    assert "'sim'" in run_sql and run_params[3] == "sim:short_put"
    trades = conn.log[1][2]
    assert json.loads(trades[0][7]) == [{"ticker": "O:NVDA"}]  # legs serialized
    assert conn.log[2][2][0][1:] == ("2025-01-02", 100000.0, 2400.0, 1)
    assert (conn.commits, conn.rollbacks) == (1, 0)


def test_persist_trades_false_keeps_the_curve_only() -> None:
    conn = _Conn()
    _store(conn, persist_trades=False)
    kinds = [entry[1] for entry in conn.log if entry[0] == "executemany"]
    assert len(kinds) == 1 and "backtest_equity" in kinds[0]


def test_failure_rolls_back_everything() -> None:
    conn = _Conn(fail_on="backtest_trade")
    with pytest.raises(RuntimeError):
        _store(conn)
    assert (conn.commits, conn.rollbacks) == (0, 1)
