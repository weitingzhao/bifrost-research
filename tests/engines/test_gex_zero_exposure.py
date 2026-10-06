"""GEX writes no levels for an expiry without gamma exposure (TD-136), and no wall
for a side without it (TD-157)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable

from bifrost_research.engines.gex import exposure
from bifrost_research.engines.gex import zero_exposure_purge as purge

TD = date(2026, 10, 2)
NEAR = date(2026, 10, 16)
FAR = date(2026, 11, 20)


class _Conn:
    """Records every statement; ``answer`` maps a statement to the rows it returns."""

    def __init__(self, answer: Callable[[str], list[Any]] | None = None) -> None:
        self.answer = answer or (lambda sql: [])
        self.statements: list[tuple[str, Any]] = []
        self.log: list[str] = []
        self._rows: list[Any] = []
        self.rowcount = 0
        self.description: tuple[Any, ...] = ()

    def cursor(self) -> _Conn:
        return self

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.statements.append((sql, params))
        self.log.append("delete" if sql.lstrip().upper().startswith("DELETE") else "select")
        self._rows = list(self.answer(sql))
        writes = ("DELETE", "UPDATE")
        self.rowcount = self._rows[0][0] if self._rows and sql.lstrip().upper().startswith(writes) else 0

    def executemany(self, sql: str, params_seq: Any) -> None:
        self.statements.append((sql, list(params_seq)))
        self.log.append("write")

    def fetchall(self) -> list[Any]:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def commit(self) -> None:
        self.log.append("commit")

    def rollback(self) -> None:
        self.log.append("rollback")


def _answer(contracts: list[Any]) -> Callable[[str], list[Any]]:
    def answer(sql: str) -> list[Any]:
        if "raw_market.stock_daily" in sql:
            return [(12.0,)]
        if "raw_market.option_open_interest" in sql:
            return contracts
        return []

    return answer


# (expiry, strike, right, open_interest, gamma, day_volume)
_LIVE = [(NEAR, 12.5, "C", 120, 0.2, 30), (NEAR, 12.5, "P", 80, 0.2, 10)]
_ZERO_OI = [(FAR, 12.5, "C", 0, 0.1, 5), (FAR, 15.0, "P", 0, 0.1, 0)]


def _written(conn: _Conn, table: str) -> list[Any]:
    return [row for sql, rows in conn.statements if sql.startswith("INSERT") and table in sql for row in rows]


def test_an_expiry_whose_open_interest_is_all_zero_writes_nothing() -> None:
    conn = _Conn(_answer(_LIVE + _ZERO_OI))
    out = exposure.compute_gex_for_symbol(conn, symbol="CTVA", trade_date=TD)
    assert out["ok"] and out["expiries"] == 1 and out["expiries_without_exposure"] == 1
    assert {r[2] for r in _written(conn, "option_metric_gex_levels_daily")} == {NEAR}
    assert {r[2] for r in _written(conn, "option_metric_gex_daily")} == {NEAR}
    assert [x for x in conn.log if x != "select"] == ["delete", "delete", "write", "write", "commit"]


def test_a_session_without_any_exposure_is_cleared_and_says_why() -> None:
    conn = _Conn(_answer(_ZERO_OI))
    out = exposure.compute_gex_for_symbol(conn, symbol="CTVA", trade_date=TD)
    assert not out["ok"] and out["error"] == "No gamma exposure"
    assert [x for x in conn.log if x != "select"] == ["delete", "delete", "commit"]


def test_has_gamma_exposure_reads_either_wall() -> None:
    assert not exposure.has_gamma_exposure({"call_wall_gex": 0.0, "put_wall_gex": 0.0})
    assert not exposure.has_gamma_exposure({"call_wall_gex": None, "put_wall_gex": None})
    assert exposure.has_gamma_exposure({"call_wall_gex": 0.0, "put_wall_gex": -3.2})
    assert exposure.has_gamma_exposure(exposure.compute_gex_levels([], 12.0)) is False


def test_intraday_writes_nothing_without_exposure() -> None:
    def answer(sql: str) -> list[Any]:
        if "raw_market.option_open_interest" in sql:
            return _ZERO_OI
        if "close" in sql and "stock_daily" in sql:
            return [(TD, 12.0)]
        return []

    conn = _Conn(answer)
    original = exposure.fetch_spot_reading
    exposure.fetch_spot_reading = lambda *a, **k: (12.0, "prior_close", TD)  # type: ignore[assignment]
    try:
        out = exposure.compute_gex_intraday(
            conn, symbol="CTVA", trade_date=TD, asof_ts=datetime(2026, 10, 2, 15, tzinfo=timezone.utc)
        )
    finally:
        exposure.fetch_spot_reading = original  # type: ignore[assignment]
    assert out == {"ok": False, "error": "No gamma exposure", "symbol": "CTVA"}
    assert "write" not in conn.log


def _purge_answer(sql: str) -> list[Any]:
    if "COUNT(DISTINCT (symbol, trade_date))" in sql:
        return [(1534, 1186, 496)]
    if "SELECT COUNT(*) FROM features.option_metric_gex_daily" in sql:
        return [(24413,)]
    if "FROM features.stock_forecast_terrain_daily t" in sql:
        return [("CTVA", TD)]
    if "FROM features.stock_signal_scan_daily s" in sql:
        return [("CTVA", TD), ("GOOG", date(2026, 7, 14))]
    if sql.lstrip().startswith("DELETE FROM features.option_metric_gex_levels_daily"):
        return [(1534,)]
    if sql.lstrip().startswith("DELETE FROM features.option_metric_gex_daily"):
        return [(24413,)]
    return []


def test_the_purge_counts_without_writing() -> None:
    conn = _Conn(_purge_answer)
    summary = purge.run(conn, apply=False)
    assert summary["gex"] == {"levels_rows": 1534, "sessions": 1186, "symbols": 496, "dist_rows": 24413}
    assert summary["terrain"] == {"sessions": 1}
    assert summary["scan"] == {"sessions": 2, "gex_read": 2}
    assert "delete" not in conn.log and "write" not in conn.log and "commit" not in conn.log


def test_the_purge_deletes_distribution_then_levels_in_one_transaction() -> None:
    conn = _Conn(_purge_answer)
    summary = purge.run(conn, apply=True)
    deletes = [sql.split()[2] for sql, _ in conn.statements if sql.lstrip().startswith("DELETE")]
    assert deletes == ["features.option_metric_gex_daily", "features.option_metric_gex_levels_daily"]
    first_commit = conn.log.index("commit")
    assert conn.log[:first_commit].count("delete") == 2
    assert summary["gex"]["levels_deleted"] == 1534 and summary["gex"]["dist_deleted"] == 24413


def test_the_purge_refuses_a_delete_that_does_not_match_its_count() -> None:
    def answer(sql: str) -> list[Any]:
        if sql.lstrip().startswith("DELETE FROM features.option_metric_gex_levels_daily"):
            return [(1600,)]
        return _purge_answer(sql)

    conn = _Conn(answer)
    try:
        purge.run(conn, apply=True)
    except RuntimeError as exc:
        assert "1534" in str(exc) and "1600" in str(exc)
    else:
        raise AssertionError("expected the purge to stop")
    assert conn.log[-1] == "rollback"


# ─── TD-157: one side without exposure ───

_PUTS_ONLY = [(NEAR, 12.5, "P", 300, 0.2, 10), (NEAR, 10.0, "P", 50, 0.1, 0), (NEAR, 15.0, "C", 0, 0.1, 0)]


def test_a_side_without_exposure_names_no_wall() -> None:
    conn = _Conn(_answer(_PUTS_ONLY))
    out = exposure.compute_gex_for_symbol(conn, symbol="CWBC", trade_date=TD)
    assert out["ok"]
    (row,) = _written(conn, "option_metric_gex_levels_daily")
    # (symbol, trade_date, expiry, spot, total, zero_gamma, call_wall, put_wall, call_wall_gex, put_wall_gex, at)
    assert row[6] is None and row[8] is None
    assert row[7] == 12.5 and row[9] < 0


def test_drop_empty_side_walls_keeps_a_side_with_exposure() -> None:
    both = {"major_call_wall": 15.0, "major_put_wall": 10.0, "call_wall_gex": 4.0, "put_wall_gex": -2.0}
    assert exposure.drop_empty_side_walls(both) == both
    calls_only = {"major_call_wall": 15.0, "major_put_wall": 10.0, "call_wall_gex": 4.0, "put_wall_gex": 0.0}
    assert exposure.drop_empty_side_walls(calls_only) == {
        "major_call_wall": 15.0,
        "major_put_wall": None,
        "call_wall_gex": 4.0,
        "put_wall_gex": None,
    }


def _one_sided_answer(call_cleared: int = 820, put_cleared: int = 942) -> Callable[[str], list[Any]]:
    def answer(sql: str) -> list[Any]:
        if "COUNT(*) FILTER" in sql:
            return [(820, 942, 754, 244)]
        if "FROM features.stock_forecast_terrain_daily t" in sql:
            return [("HON", TD), ("SM", date(2026, 9, 30))]
        if "JOIN unnest" in sql and "stock_signal_scan_daily" in sql:
            return [("HON", TD)]
        if sql.lstrip().startswith("UPDATE") and "major_call_wall = NULL" in sql:
            return [(call_cleared,)]
        if sql.lstrip().startswith("UPDATE") and "major_put_wall = NULL" in sql:
            return [(put_cleared,)]
        return []

    return answer


def test_the_one_sided_pass_counts_without_writing() -> None:
    conn = _Conn(_one_sided_answer())
    summary = purge.run_one_sided(conn, apply=False)
    assert summary["gex"] == {"call_walls": 820, "put_walls": 942, "sessions": 754, "symbols": 244}
    assert summary["terrain"] == {"sessions": 2} and summary["scan"] == {"sessions": 1}
    assert not any(sql.lstrip().startswith(("UPDATE", "DELETE")) for sql, _ in conn.statements)
    assert "commit" not in conn.log


def test_the_one_sided_pass_clears_both_sides_then_commits() -> None:
    conn = _Conn(_one_sided_answer())
    summary = purge.run_one_sided(conn, apply=True)
    updates = [sql for sql, _ in conn.statements if sql.lstrip().startswith("UPDATE")]
    assert len(updates) == 2 and "major_call_wall IS NOT NULL" in updates[0] and "major_put_wall IS NOT NULL" in updates[1]
    assert summary["gex"]["call_walls_cleared"] == 820 and summary["gex"]["put_walls_cleared"] == 942
    assert "commit" in conn.log and "rollback" not in conn.log


def test_the_one_sided_pass_refuses_an_update_that_does_not_match_its_count() -> None:
    conn = _Conn(_one_sided_answer(put_cleared=1000))
    try:
        purge.run_one_sided(conn, apply=True)
    except RuntimeError as exc:
        assert "820/942" in str(exc) and "820/1000" in str(exc)
    else:
        raise AssertionError("expected the pass to stop")
    assert conn.log[-1] == "rollback" and "commit" not in conn.log
