"""Adjusted option contracts stay out of every reader of raw option data (0.154.0),
and the readers that only upserted now replace a name's session."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from bifrost_research.engines import flow
from bifrost_research.engines.adjusted_contracts import not_adjusted_contract_sql
from bifrost_research.engines.gex import exposure
from bifrost_research.engines.backtest import event_query
from bifrost_research.engines.volatility import atm_iv, iv_coverage_heal, iv_history_repair, iv_solver, max_pain, pcr, surface

TD = date(2026, 9, 29)


class _Conn:
    """Records every statement; ``answer`` maps a statement to the rows it returns."""

    def __init__(self, answer: Callable[[str], list[Any]] | None = None) -> None:
        self.answer = answer or (lambda sql: [])
        self.statements: list[tuple[str, Any]] = []
        self.log: list[str] = []
        self._rows: list[Any] = []
        self.rowcount = 0

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


def test_the_predicate_reads_the_root_off_the_named_column() -> None:
    assert not_adjusted_contract_sql("oi.option_ticker") == (
        "substr(oi.option_ticker, 3, length(oi.option_ticker) - 17) !~ '[0-9]$'"
    )
    assert surface.NOT_ADJUSTED_CONTRACT_SQL == not_adjusted_contract_sql("v.option_ticker")


def _raw_reads(conn: _Conn) -> list[str]:
    return [
        sql
        for sql, _ in conn.statements
        if any(t in sql for t in ("raw_market.option_", "v_option_snapshot", "option_iv_reconstructed_daily"))
        # iv_solver's own lookup of the vendor keys it already stored, to skip them
        and "solver_status = 'vendor_snapshot'" not in sql
    ]


def test_every_raw_reader_leaves_adjusted_contracts_out() -> None:
    calls: list[tuple[Callable[[_Conn], Any], str]] = [
        (lambda c: iv_solver.project_vendor_snapshot_window(c, "APTV", TD, TD, dry_run=True), "v.option_ticker"),
        (lambda c: iv_solver.solve_symbol_window(c, "APTV", TD, TD, dry_run=True), "o.option_ticker"),
        (lambda c: iv_solver.degraded_sessions(c, "APTV", TD, TD), "os.option_ticker"),
        (lambda c: atm_iv.fetch_reconstructed_iv_rows_for_date(c, TD, underlyings=["APTV"]), "option_ticker"),
        (lambda c: atm_iv.fetch_reconstructed_iv_rows_for_date(c, TD), "option_ticker"),
        (lambda c: atm_iv.fetch_snapshot_iv_rows_for_date(c, TD, underlyings=["APTV"]), "v.option_ticker"),
        (lambda c: atm_iv.fetch_option_daily_brent_rows_for_date(c, TD, underlyings=["APTV"]), "o.option_ticker"),
        (lambda c: exposure.fetch_gex_contracts(c, "APTV", TD), "oi.option_ticker"),
        (lambda c: exposure.fetch_gex_contracts(c, "APTV", TD, expiry=date(2026, 10, 16)), "oi.option_ticker"),
        (lambda c: exposure.parity_spot(c, "APTV", TD), "os.option_ticker"),
        (lambda c: flow.fetch_flow_rows(c, "APTV", TD), "oi.option_ticker"),
        (lambda c: pcr.fetch_oi_totals_for_date(c, TD, underlyings=["APTV"]), "option_ticker"),
        (lambda c: pcr.fetch_oi_totals_for_date(c, TD), "option_ticker"),
        (lambda c: pcr.fetch_volume_totals_for_date(c, TD, underlyings=["APTV"]), "os.option_ticker"),
        # 0.155.0
        (lambda c: max_pain.fetch_oi_rows_for_date(c, TD, underlyings=["APTV"]), "option_ticker"),
        (lambda c: max_pain.fetch_oi_rows_for_date(c, TD), "option_ticker"),
        (lambda c: surface.fetch_spot_fallback(c, "APTV", TD), "os.option_ticker"),
        (lambda c: iv_coverage_heal.raw_breadth(c, ["APTV"], TD, TD), "option_ticker"),
        (lambda c: iv_history_repair.reproject_vendor(c, ["APTV"], TD, apply=False), "option_ticker"),
        (lambda c: event_query._pick_option(c, "APTV", TD, "C", 30, 35.0), "option_ticker"),
    ]
    for call, column in calls:
        conn = _Conn()
        call(conn)
        reads = _raw_reads(conn)
        assert any(not_adjusted_contract_sql(column) in sql for sql in reads), column
        for sql in reads:  # including a reader's own helpers (the degraded-session check)
            assert "length(" in sql and "- 17) !~ '[0-9]$'" in sql, sql
        for sql, params in conn.statements:
            assert sql.count("%s") == len(params or ()), sql


def test_the_max_pain_endpoints_leave_adjusted_contracts_out() -> None:
    from bifrost_research.api import options

    def answer(sql: str) -> list[Any]:
        return [(TD,)] if "MAX(trade_date)" in sql else []

    for call in (
        lambda c: options.compute_max_pain_live(c, symbol="APTV", expiry=date(2026, 10, 16)),
        lambda c: options.compute_max_pain_history(c, symbol="APTV", expiry=date(2026, 10, 16)),
    ):
        conn = _Conn(answer)
        call(conn)
        reads = _raw_reads(conn)
        assert len(reads) == 2
        assert all(not_adjusted_contract_sql("option_ticker") in sql for sql in reads)


def test_the_tape_reader_leaves_adjusted_contracts_out() -> None:
    conn = _Conn(lambda sql: [(1,)] if "information_schema" in sql else [])
    flow.fetch_tape_flow_rows(conn, "APTV", TD)
    tape = [sql for sql, _ in conn.statements if "raw_market.option_trades" in sql]
    assert len(tape) == 1 and not_adjusted_contract_sql("t.option_ticker") in tape[0]


_OI_ROW = (date(2026, 10, 16), 35.0, "C", 120, 0.05, 30)


def _gex_answer(contracts: list[Any]) -> Callable[[str], list[Any]]:
    def answer(sql: str) -> list[Any]:
        if "raw_market.stock_daily" in sql:
            return [(35.0,)]
        if "raw_market.option_open_interest" in sql:
            return contracts
        return []

    return answer


def test_gex_replaces_the_session_in_one_transaction() -> None:
    conn = _Conn(_gex_answer([_OI_ROW]))
    result = exposure.compute_gex_for_symbol(conn, symbol="aptv", trade_date=TD)
    assert result["ok"]
    deletes = [(sql, p) for sql, p in conn.statements if sql.startswith("DELETE")]
    assert [d[0].split()[2] for d in deletes] == [
        "features.option_metric_gex_daily",
        "features.option_metric_gex_levels_daily",
    ]
    assert all(p == ("APTV", TD) for _, p in deletes)
    tail = [x for x in conn.log if x != "select"]
    assert tail == ["delete", "delete", "write", "write", "commit"]


def test_gex_clears_a_session_whose_contracts_are_all_gone() -> None:
    conn = _Conn(_gex_answer([]))
    result = exposure.compute_gex_for_symbol(conn, symbol="CUE", trade_date=TD)
    assert not result["ok"]
    assert [x for x in conn.log if x != "select"] == ["delete", "delete", "commit"]


def test_gex_for_one_expiry_replaces_only_that_expiry() -> None:
    exp = date(2026, 10, 16)
    conn = _Conn(_gex_answer([_OI_ROW]))
    exposure.compute_gex_for_symbol(conn, symbol="APTV", trade_date=TD, expiry=exp)
    deletes = [(sql, p) for sql, p in conn.statements if sql.startswith("DELETE")]
    assert all(sql.endswith("AND expiry = %s") and p == ("APTV", TD, exp) for sql, p in deletes)


def test_flow_replaces_the_session_and_clears_an_empty_one() -> None:
    row = (date(2026, 10, 16), 35.0, "C", 500, 120, 1.2, 1.1)
    conn = _Conn(lambda sql: [row] if "raw_market.option_open_interest" in sql else [])
    assert flow.compute_order_flow_for_symbol(conn, symbol="aptv", trade_date=TD)["ok"]
    deletes = [(sql, p) for sql, p in conn.statements if sql.startswith("DELETE")]
    assert [d[0].split()[2] for d in deletes] == [
        "features.option_flow_sentiment_daily",
        "features.option_flow_multi_leg_daily",
    ]
    assert all(p == ("APTV", TD) for _, p in deletes)
    assert [x for x in conn.log if x != "select"] == ["delete", "delete", "write", "commit"]

    empty = _Conn()
    assert not flow.compute_order_flow_for_symbol(empty, symbol="CUE", trade_date=TD)["ok"]
    assert [x for x in empty.log if x != "select"] == ["delete", "delete", "commit"]


def test_pcr_replaces_every_symbol_asked_for_even_without_rows() -> None:
    def answer(sql: str) -> list[Any]:
        if "raw_market.option_open_interest" in sql:
            return [("APTV", "P", 200), ("APTV", "C", 100)]
        return []

    conn = _Conn(answer)
    pcr.compute_pcr_for_date(conn, trade_date=TD, underlyings=["APTV", "CUE"])
    deletes = [(sql, p) for sql, p in conn.statements if sql.startswith("DELETE")]
    assert deletes == [
        ("DELETE FROM features.option_metric_pcr_daily WHERE trade_date = %s AND symbol = ANY(%s)", (TD, ["APTV", "CUE"]))
    ]
    assert [x for x in conn.log if x != "select"] == ["delete", "write", "commit"]

    empty = _Conn()
    pcr.compute_pcr_for_date(empty, trade_date=TD, underlyings=["CUE"])
    assert [x for x in empty.log if x != "select"] == ["delete", "commit"]


def _oi(sym: str, strike: float, right: str, oi: int, expiry: date = date(2026, 10, 16)) -> tuple[Any, ...]:
    return (sym, expiry, strike, right, oi)


def test_max_pain_replaces_every_symbol_asked_for_in_one_transaction() -> None:
    rows = [_oi("APTV", 35.0, "C", 10), _oi("APTV", 35.0, "P", 10)]
    conn = _Conn(lambda sql: rows if "raw_market.option_open_interest" in sql else [])
    out = max_pain.compute_max_pain_for_date(conn, trade_date=TD, underlyings=["APTV", "CUE"])
    deletes = [(sql, p) for sql, p in conn.statements if sql.startswith("DELETE")]
    assert deletes == [
        (
            "DELETE FROM features.option_metric_max_pain_daily WHERE trade_date = %s AND symbol = ANY(%s)",
            (TD, ["APTV", "CUE"]),
        )
    ]
    assert out["groups"] == 1
    assert [x for x in conn.log if x != "select"] == ["delete", "write", "commit"]


def test_max_pain_skips_an_expiry_whose_open_interest_is_zero() -> None:
    later = date(2026, 11, 20)
    rows = [
        _oi("CWBC", 12.5, "C", 5),
        _oi("CWBC", 15.0, "P", 3),
        _oi("CWBC", 15.0, "C", 0, later),
        _oi("CWBC", 17.5, "P", 0, later),
    ]
    conn = _Conn(lambda sql: rows if "raw_market.option_open_interest" in sql else [])
    out = max_pain.compute_max_pain_for_date(conn, trade_date=TD, underlyings=["CWBC"])
    assert out["groups"] == 1 and out["skipped_zero_oi"] == 1
    written = next(p for sql, p in conn.statements if sql.startswith("INSERT"))
    assert [r[2] for r in written] == [date(2026, 10, 16)]


def test_max_pain_keeps_a_session_with_no_open_interest_at_all() -> None:
    conn = _Conn()
    max_pain.compute_max_pain_for_date(conn, trade_date=TD, underlyings=["APTV"])
    assert "delete" not in conn.log and "write" not in conn.log


def test_atm_replaces_a_symbol_asked_for_that_has_no_source_rows() -> None:
    recon = [
        ("O:PLTR261016C00100000", "PLTR", 0.50, 100.0, date(2026, 10, 16), 100.0, "C", "vendor_snapshot"),
        ("O:PLTR261016P00100000", "PLTR", 0.52, 100.0, date(2026, 10, 16), 100.0, "P", "vendor_snapshot"),
    ]
    conn = _Conn(lambda sql: recon if "option_iv_reconstructed_daily" in sql else [])
    atm_iv.compute_atm_iv_for_date(conn, trade_date=TD, underlyings=["PLTR", "LEN"])
    delete = next(p for sql, p in conn.statements if "DELETE FROM" in sql)
    assert delete == (TD, ["LEN", "PLTR"])


# ─── the one-off rebuild (adjusted_contract_purge) ───

from bifrost_research.engines import adjusted_contract_purge as purge  # noqa: E402


def test_purge_counts_without_writing() -> None:
    def answer(sql: str) -> list[Any]:
        if "SELECT COUNT(*)" in sql:
            return [(2385,)]
        if "SELECT DISTINCT symbol, trade_date FROM features.option_iv_reconstructed_daily" in sql:
            return [("APTV", TD)]
        if "JOIN unnest" in sql:
            return [("APTV", TD)]
        return []

    conn = _Conn(answer)
    summary = purge.run(conn, apply=False)
    assert summary["reconstructed"] == {"rows": 2385, "sessions": 1}
    assert summary["atm"]["sessions"] == 1
    assert "delete" not in conn.log and "write" not in conn.log and "commit" not in conn.log
    for sql, _ in conn.statements:
        if "raw_market" in sql or "option_iv_reconstructed_daily" in sql and "JOIN unnest" not in sql:
            assert "NOT (substr(" in sql, sql


def test_a_moved_iv30_includes_one_that_became_empty() -> None:
    a, b, c = ("CDE", date(2025, 4, 17)), ("SM", date(2026, 9, 3)), ("HON", date(2026, 9, 8))
    before = {a: 3.335, b: 1.325, c: 0.30}
    after = {a: None, b: 0.46, c: 0.30}
    assert purge._moved(before, after) == {a, b}


def test_downstream_takes_every_session_whose_window_holds_a_moved_iv30() -> None:
    days = [date(2025, 1, 1 + i) for i in range(10)]

    def answer(sql: str) -> list[Any]:
        return [(d,) for d in days] if "SELECT trade_date FROM" in sql else []

    original = purge.WINDOW
    purge.WINDOW = 3
    try:
        out = purge.downstream_sessions(_Conn(answer), [("LEN", days[2])])
    finally:
        purge.WINDOW = original
    assert sorted(d for _, d in out) == days[2:5]


def test_purge_clears_an_atm_session_whose_source_is_now_empty() -> None:
    """LEN 2025-01-21: every near-money contract was adjusted, so the recompute finds
    nothing and returns before its own delete; the purge clears the session itself."""
    day = date(2025, 1, 21)

    def answer(sql: str) -> list[Any]:
        if "SELECT COUNT(*)" in sql:
            return [(0,)]
        if "FROM raw_market.option_daily" in sql and "SELECT DISTINCT underlying, bar_date" in sql:
            return [("LEN", day)]
        if "JOIN unnest" in sql and "SELECT DISTINCT t.symbol" in sql:
            return [("LEN", day)]
        return []

    conn = _Conn(answer)
    purge.run(conn, apply=True)
    atm_deletes = [
        (sql, p) for sql, p in conn.statements if sql.startswith("DELETE FROM features.option_metric_atm_iv_daily")
    ]
    assert atm_deletes == [
        ("DELETE FROM features.option_metric_atm_iv_daily WHERE trade_date = %s AND symbol = ANY(%s)", (day, ["LEN"]))
    ]
    i = conn.log.index("delete", conn.log.index("commit"))  # the first commit closes the reconstructed delete
    assert "commit" in conn.log[i + 1 :]


def test_the_max_pain_pass_counts_without_writing() -> None:
    def answer(sql: str) -> list[Any]:
        if "SELECT COUNT(*)" in sql:
            return [(842,)]
        if "FROM raw_market.option_open_interest" in sql:
            return [("CUE", date(2026, 9, 25)), ("HON", date(2026, 9, 16))]
        if "JOIN unnest" in sql and "option_metric_max_pain_daily" in sql:
            return [("CUE", date(2026, 9, 25)), ("HON", date(2026, 9, 16))]
        if "JOIN unnest" in sql and "stock_forecast_terrain_daily" in sql:
            return [("HON", date(2026, 9, 16))]
        return []

    conn = _Conn(answer)
    summary = purge.run_max_pain(conn, apply=False, zero_oi=True)
    assert summary["max_pain"]["sessions"] == 2
    assert summary["terrain"] == {"sessions": 1, "listed": 95}
    assert summary["zero_oi"] == {"rows": 842, "delete": True}
    assert "delete" not in conn.log and "write" not in conn.log and "commit" not in conn.log


def test_the_max_pain_pass_clears_cue_although_its_recompute_finds_nothing() -> None:
    day = date(2026, 9, 25)

    def answer(sql: str) -> list[Any]:
        if "SELECT COUNT(*)" in sql:
            return [(0,)]
        if "SELECT DISTINCT underlying, trade_date FROM raw_market.option_open_interest" in sql:
            return [("CUE", day)]
        if "JOIN unnest" in sql and "SELECT DISTINCT t.symbol" in sql and "max_pain" in sql:
            return [("CUE", day)]
        return []

    conn = _Conn(answer)
    summary = purge.run_max_pain(conn, apply=True)
    deletes = [(sql, p) for sql, p in conn.statements if sql.startswith("DELETE")]
    assert deletes == [
        ("DELETE FROM features.option_metric_max_pain_daily WHERE trade_date = %s AND symbol = ANY(%s)", (day, ["CUE"]))
    ]
    assert conn.log[conn.log.index("delete") + 1 :].count("commit") >= 1
    assert "deleted" not in summary["zero_oi"]


def test_terrain_pairs_are_the_95_measured() -> None:
    pairs = purge.terrain_pairs()
    assert len(pairs) == 95
    assert ("HON", date(2026, 9, 16)) in pairs and ("HONA", date(2026, 9, 16)) in pairs
