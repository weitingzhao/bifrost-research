"""IV history repair: chunking, scope parsing, and the purge's two refusals."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from bifrost_research.engines.volatility import iv_history_repair as repair


class _Cur:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.rowcount = 0
        self._one: Any = None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.sql.append(sql)
        if "MIN(DATE(timezone" in sql:
            self._one = (self.conn.raw_first,)
        elif "MIN(trade_date)" in sql:
            self._one = (self.conn.oldest,)
        elif sql.lstrip().startswith("SELECT COUNT(*)"):
            self._one = (self.conn.counts.pop(0) if self.conn.counts else 0,)
        elif sql.lstrip().startswith("DELETE"):
            self.rowcount = self.conn.deleted

    def fetchone(self) -> Any:
        return self._one

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *a: object) -> None:
        return None


class _Conn:
    def __init__(self, *, raw_first: date | None, oldest: date | None, counts: list[int], deleted: int = 0) -> None:
        self.raw_first, self.oldest, self.counts, self.deleted = raw_first, oldest, counts, deleted
        self.sql: list[str] = []
        self.commits = self.rollbacks = 0

    def cursor(self) -> _Cur:
        return _Cur(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_windows_cover_the_range_without_overlap() -> None:
    chunks = list(repair.windows(date(2026, 8, 1), date(2026, 8, 20), 7))
    assert chunks == [
        (date(2026, 8, 1), date(2026, 8, 7)),
        (date(2026, 8, 8), date(2026, 8, 14)),
        (date(2026, 8, 15), date(2026, 8, 20)),
    ]


def test_solve_scope_lists() -> None:
    assert repair.solve_scope(None, "none", ["SPY"]) == []
    assert repair.solve_scope(None, "universe", ["SPY", "QQQ"]) == ["SPY", "QQQ"]
    assert repair.solve_scope(None, " pltr,NVDA ,,", []) == ["NVDA", "PLTR"]


def test_purge_refuses_once_raw_is_trimmed_past_the_first_observation() -> None:
    conn = _Conn(raw_first=date(2026, 9, 1), oldest=date(2025, 6, 2), counts=[])
    with pytest.raises(RuntimeError, match="refusing"):
        repair.purge_fossils(conn, apply=True)


def test_purge_rolls_back_when_the_delete_touches_a_different_count() -> None:
    conn = _Conn(raw_first=repair.FIRST_OBSERVATION, oldest=date(2026, 9, 1), counts=[40], deleted=41)
    with pytest.raises(RuntimeError, match="rolled back"):
        repair.purge_fossils(conn, apply=True)
    assert conn.rollbacks == 1
    assert conn.commits == 0


def test_purge_dry_run_only_counts() -> None:
    conn = _Conn(raw_first=repair.FIRST_OBSERVATION, oldest=date(2026, 9, 1), counts=[5, 3, 0])
    out = repair.purge_fossils(conn, apply=False)
    assert out["rows"] == 5 + 3
    assert not any(s.lstrip().startswith("DELETE") for s in conn.sql)
