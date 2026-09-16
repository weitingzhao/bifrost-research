"""Terrain backfill — targets from the Trade API, and no regime without its inputs (R9 F3)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from bifrost_research.engines.forecast import terrain_backfill as tb

TODAY = date(2026, 9, 15)
MARCH = date(2026, 3, 12)
AUGUST = date(2026, 8, 20)


def _epoch(day: date) -> float:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()


def _get(attributions: list[dict[str, Any]], executions: list[dict[str, Any]]) -> Any:
    def _inner(base: str, path: str, params: Any = None) -> Any:
        if path == "/executions/position-attribution":
            return {"attributions": attributions}
        assert path == "/executions" and params["since_ts"] == 0
        return {"executions": executions}

    return _inner


def test_targets_are_the_opening_sessions_of_instances_that_no_longer_hold() -> None:
    executions = [
        {"strategy_instance_id": 1, "symbol": "NVDA  260320C00120000",
         "strategy_instance_opened_at_epoch": _epoch(MARCH), "trade_date": "2026-03-12"},
        {"strategy_instance_id": 1, "symbol": "NVDA", "trade_date": "2026-03-13",
         "strategy_instance_opened_at_epoch": _epoch(MARCH)},
        {"strategy_instance_id": 2, "symbol": "MU    260821P00180000",
         "strategy_instance_opened_at_epoch": _epoch(AUGUST), "trade_date": "2026-08-20"},
        # No instance link — cannot be attributed to an opening session.
        {"strategy_instance_id": None, "symbol": "SPY", "trade_date": "2026-08-20"},
    ]
    attributions = [{"strategy_instance_id": 2}]
    targets, stats = tb.instance_targets(_get(attributions, executions), "http://trade")
    assert stats == {
        "instances_with_executions": 2,
        "instances_holding": 1,
        "closed_instances": 1,
        "targets": 1,
    }
    # Instance 2 still holds, so only instance 1's opening session is a target, and
    # the OCC local symbol resolves to the underlying.
    assert targets == [("NVDA", MARCH)]


def test_an_instance_without_an_opened_at_falls_back_to_its_earliest_trade() -> None:
    executions = [
        {"strategy_instance_id": 7, "symbol": "AMD", "trade_date": "2026-04-10"},
        {"strategy_instance_id": 7, "symbol": "AMD", "trade_date": "2026-04-02"},
    ]
    targets, _ = tb.instance_targets(_get([], executions), "http://trade")
    assert targets == [("AMD", date(2026, 4, 2))]


def test_an_input_that_does_not_reach_the_session_blocks_it() -> None:
    on = MARCH
    # GEX only starts in May, momentum and IV cover March.
    dates = {"gex": None, "momentum": date(2026, 3, 12), "iv": date(2026, 3, 11)}
    assert tb.usable(dates, on) == ["gex"]
    # A row a week old still counts; older does not.
    assert tb.usable({"gex": on - timedelta(days=7), "momentum": on, "iv": on}, on) == []
    assert tb.usable({"gex": on - timedelta(days=8), "momentum": on, "iv": on}, on) == ["gex"]
    assert tb.usable({"gex": None, "momentum": None, "iv": None}, on) == ["gex", "momentum", "iv"]


class _Cur:
    def __init__(self, owner: _Conn) -> None:
        self.owner = owner

    def execute(self, sql: str, params: Any = None) -> None:
        self.owner.last = (sql, params)
        self.owner.sql.append(sql)

    def executemany(self, sql: str, rows: Any) -> None:
        self.owner.written.extend(list(rows))

    def fetchone(self) -> tuple[Any, ...] | None:
        sql, params = self.owner.last
        if "MIN(trade_date)" in sql or "MIN(bar_date)" in sql:
            return (date(2026, 5, 4),)
        if "MAX(trade_date)" in sql:
            symbol, on = params
            table_key = next(k for k, t in tb.SIGNAL_INPUTS if t in sql)
            return (self.owner.coverage.get(table_key),)
        if "stock_forecast_terrain_daily" in sql:
            return (1,) if (params[0], params[1]) in self.owner.existing else None
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(
        self,
        coverage: dict[str, date | None],
        existing: set[tuple[str, date]] | None = None,
    ) -> None:
        self.coverage = coverage
        self.existing = existing or set()
        self.last: tuple[str, Any] = ("", None)
        self.sql: list[str] = []
        self.written: list[Any] = []
        self.commits = 0

    def cursor(self) -> _Cur:
        return _Cur(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_a_session_with_no_gex_is_reported_not_scored(monkeypatch) -> None:
    conn = _Conn({"gex": None, "momentum": MARCH, "iv": MARCH})
    called: list[str] = []
    monkeypatch.setattr(
        tb, "load_upstream_signals", lambda *a, **k: called.append("loaded") or (0.0, {}, {}, {})
    )
    result = tb.backfill(conn, [("NVDA", MARCH)])
    assert result["inputs_reached"] == 0 and result["skipped"] == 1 and result["coverage"] == 0.0
    assert result["skip_reasons"] == {"missing:gex": 1}
    assert result["skipped_sample"][0] == {
        "symbol": "NVDA",
        "trade_date": "2026-03-12",
        "missing": ["gex"],
    }
    # The terrain engine was never asked to score a regime out of defaults.
    assert called == []
    assert conn.written == []


def test_a_session_whose_inputs_are_all_there_is_written(monkeypatch) -> None:
    conn = _Conn({"gex": AUGUST, "momentum": AUGUST, "iv": AUGUST})
    monkeypatch.setattr(
        tb,
        "load_upstream_signals",
        lambda *a, **k: (
            230.0,
            {"zero_gamma": 228.0, "major_call_wall": 240.0, "major_put_wall": 220.0,
             "total_net_gex": -1.0e8, "spot": 230.0},
            {"score": 71.0, "path": "EXT", "crash": False},
            {"iv_percentile_1y": 55.0, "iv_rank_1y": 52.0},
        ),
    )
    written: list[Any] = []
    monkeypatch.setattr(
        tb, "upsert_market_terrain", lambda conn, rows: written.extend(rows) or len(rows)
    )
    result = tb.backfill(conn, [("NVDA", AUGUST)])
    assert result["inputs_reached"] == 1 and result["rows_written"] == 1 and result["coverage"] == 1.0
    # No row existed, so this is a session gained, not one rewritten.
    assert result["new_rows"] == 1 and result["rewritten_rows"] == 0
    assert result["covered_sample"][0]["regime"] in ("crash-risk", "range", "trending")
    assert result["covered_sample"][0]["was"] == "new"
    assert written and written[0].symbol == "NVDA" and written[0].trade_date == AUGUST
    assert conn.commits == 1


def _signals(monkeypatch) -> list[Any]:
    """Upstream signals that support a regime, plus a spy on what gets written."""
    monkeypatch.setattr(
        tb,
        "load_upstream_signals",
        lambda *a, **k: (
            230.0,
            {"zero_gamma": 228.0, "major_call_wall": 240.0, "major_put_wall": 220.0,
             "total_net_gex": -1.0e8, "spot": 230.0},
            {"score": 71.0, "path": "EXT", "crash": False},
            {"iv_percentile_1y": 55.0, "iv_rank_1y": 52.0},
        ),
    )
    written: list[Any] = []
    monkeypatch.setattr(
        tb, "upsert_market_terrain", lambda conn, rows: written.extend(rows) or len(rows)
    )
    return written


def test_a_session_the_nightly_slot_already_wrote_is_left_alone(monkeypatch) -> None:
    # C0: the first run of this engine wrote 8 rows and every one of them was an
    # existing row rewritten. Overwriting tonight's answer has to be asked for.
    conn = _Conn({"gex": AUGUST, "momentum": AUGUST, "iv": AUGUST}, existing={("NVDA", AUGUST)})
    written = _signals(monkeypatch)
    result = tb.backfill(conn, [("NVDA", AUGUST)])
    assert result["rows_written"] == 0 and result["new_rows"] == 0 and result["rewritten_rows"] == 0
    assert result["skipped_existing"] == 1 and result["skipped_existing_sample"] == [
        {"symbol": "NVDA", "trade_date": "2026-08-20"}
    ]
    # The inputs do reach the session, so it counts towards coverage — coverage is
    # not a count of sessions gained, and the result says so.
    assert result["inputs_reached"] == 1 and result["coverage"] == 1.0
    assert "not sessions newly gained" in result["coverage_meaning"]
    assert written == []


def test_force_rewrites_an_existing_session_and_says_so(monkeypatch) -> None:
    conn = _Conn({"gex": AUGUST, "momentum": AUGUST, "iv": AUGUST}, existing={("NVDA", AUGUST)})
    written = _signals(monkeypatch)
    result = tb.backfill(conn, [("NVDA", AUGUST)], force=True)
    assert result["force"] is True and result["rows_written"] == 1
    assert result["rewritten_rows"] == 1 and result["new_rows"] == 0
    assert result["skipped_existing"] == 0
    assert result["covered_sample"][0]["was"] == "rewritten"
    assert len(written) == 1


def test_spot_that_cannot_be_found_is_a_skip_with_its_own_reason(monkeypatch) -> None:
    conn = _Conn({"gex": AUGUST, "momentum": AUGUST, "iv": AUGUST})
    monkeypatch.setattr(tb, "load_upstream_signals", lambda *a, **k: (0.0, {"spot": 0}, {"score": 1}, {"iv_rank_1y": 2}))
    result = tb.backfill(conn, [("NVDA", AUGUST)])
    assert result["skip_reasons"] == {"no_spot": 1} and result["rows_written"] == 0


def test_input_floors_are_read_from_every_input(monkeypatch) -> None:
    conn = _Conn({})
    floors = tb.input_floors(conn)
    assert set(floors) == {"gex", "momentum", "iv", "stock_daily"}
    assert floors["gex"] == "2026-05-04"


def test_a_down_trade_api_skips_the_backfill(monkeypatch) -> None:
    def _boom(base: str, path: str, params: Any = None) -> Any:
        raise RuntimeError("unreachable")

    monkeypatch.setattr("bifrost_research.mcp.tools._trade_api_client.get", _boom)
    monkeypatch.setattr(tb, "connect", lambda: (_ for _ in ()).throw(AssertionError("must not connect")))
    result = tb.run(as_of=TODAY)
    assert result["mode"] == "skipped" and result["rows_written"] == 0
