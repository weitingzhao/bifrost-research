"""A4 — fwd_ret_20d is filled in once the 20 sessions have elapsed."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from bifrost_research.engines.vrp import entry


class _Cur:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self._out: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.statements.append((sql, params))
        q = sql.lower()
        if "fwd_ret_20d is null" in q:
            self._out = list(self.conn.pending)
        elif "from raw_market.stock_daily" in q:
            sym, start = params[0], params[1]
            self._out = [(d, c) for d, c in self.conn.bars.get(sym, []) if d >= start]
        elif q.strip().startswith("update"):
            self.conn.updates.append(params)
            self._out = []
        else:
            self._out = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._out)

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self) -> None:
        self.pending: list[tuple[str, date]] = []
        self.bars: dict[str, list[tuple[date, float]]] = {}
        self.updates: list[Any] = []
        self.statements: list[tuple[str, Any]] = []
        self.commits = 0

    def cursor(self) -> _Cur:
        return _Cur(self)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        pass


def test_backfill_writes_the_20_session_log_return_and_leaves_young_rows_pending(monkeypatch) -> None:
    conn = _Conn()
    old = date(2026, 7, 1)
    young = date(2026, 8, 20)
    conn.pending = [("NVDA", old), ("NVDA", young)]
    conn.bars["NVDA"] = [(old + timedelta(days=i), 100.0 * (1.01**i)) for i in range(60)]
    monkeypatch.setattr(entry, "connect", lambda: conn)
    result = entry.backfill_fwd_ret_20d(lookback_days=90, as_of=date(2026, 9, 6))
    assert result["candidates"] == 2 and result["updated"] == 1 and result["still_pending"] == 1
    fwd, sym, td = conn.updates[0]
    assert sym == "NVDA" and td == old
    assert abs(fwd - 20 * 0.00995033) < 1e-6  # log(1.01) per session × 20
    assert conn.commits == 1
    select_sql, select_params = conn.statements[0]
    assert select_params[1] == date(2026, 9, 6) - timedelta(days=entry.MIN_AGE_DAYS_FOR_20D)
