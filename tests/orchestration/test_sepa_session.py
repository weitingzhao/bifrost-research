"""TD-87: SEPA is stamped with the session its data describes, never the UTC calendar day.

The seven SEPA marts used ``current_date``. The database clock is UTC and
research_trading_day fires at 22:30 New York = 02:30 UTC the next day, so
Monday's session was stored as Tuesday and Friday's as Saturday. These tests
hold the three layers of the fix: the marts derive the session from their source
(dbt macro ``sepa_session``), the projection writes the New York session
explicitly and refuses a mart that disagrees, and the asset check flags any
stamped date that is not a session.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from bifrost_research.db import calendar
from bifrost_research.orchestration import sepa_projection as sp

MARTS = Path(__file__).resolve().parents[2] / "src/bifrost_research/dbt/models/marts"

MON, TUE, FRI, SAT = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 2), date(2026, 10, 3)


# ── the marts ─────────────────────────────────────────────────────────────


def _sql_without_comments(path: Path) -> str:
    text = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def test_no_sepa_mart_reads_the_wall_clock() -> None:
    marts = sorted(MARTS.glob("mart_sepa_*.sql"))
    assert len(marts) >= 10
    offenders = [
        p.name
        for p in marts
        if re.search(r"\b(current_date|current_timestamp|now\(\))", _sql_without_comments(p), re.I)
    ]
    assert offenders == [], f"SEPA marts must stamp sepa_session(), not the clock: {offenders}"


def test_every_mart_that_stamps_eval_date_uses_sepa_session() -> None:
    stamping = [p for p in MARTS.glob("mart_sepa_*.sql") if re.search(r"as eval_date", p.read_text())]
    assert len(stamping) == 7
    for p in stamping:
        assert re.search(r"\{\{\s*sepa_session\(\)\s*\}\}\s+as eval_date", p.read_text()), p.name


def test_the_feature_mart_carries_the_session_test() -> None:
    yml = (MARTS / "_marts__models.yml").read_text()
    block = yml.split("- name: mart_sepa_feature_daily", 1)[1]
    trade_date = block.split("- name: trade_date", 1)[1].split("- name:", 1)[0]
    assert "sepa_session_is_newest_trading_day" in trade_date


# ── the New York session ──────────────────────────────────────────────────


@pytest.fixture
def no_holidays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calendar, "cached_closed_days", lambda _conn, _s, _e: frozenset())


@pytest.mark.parametrize(
    ("now", "session"),
    [
        # The nightly batch: 22:39 New York on Monday is 02:39 UTC on Tuesday.
        (datetime(2026, 10, 6, 2, 39, tzinfo=UTC), MON),
        # Friday night's batch runs on Saturday UTC; the session is Friday.
        (datetime(2026, 10, 3, 2, 39, tzinfo=UTC), FRI),
        # Weekend and Monday morning still describe Friday.
        (datetime(2026, 10, 4, 18, 0, tzinfo=UTC), FRI),
        (datetime(2026, 10, 5, 15, 0, tzinfo=UTC), FRI),
        # 16:05 New York on Monday: Monday has closed.
        (datetime(2026, 10, 5, 20, 5, tzinfo=UTC), MON),
    ],
)
def test_latest_closed_session_is_the_new_york_session(no_holidays: None, now: datetime, session: date) -> None:
    assert calendar.latest_closed_session(object(), now=now) == session


def test_a_holiday_is_not_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    labor_day = date(2026, 9, 7)
    monkeypatch.setattr(calendar, "cached_closed_days", lambda _conn, _s, _e: frozenset({labor_day}))
    # Labor Day night's batch (02:30 UTC Tuesday) describes Friday 09-04.
    now = datetime(2026, 9, 8, 2, 30, tzinfo=UTC)
    assert calendar.latest_closed_session(object(), now=now) == date(2026, 9, 4)


# ── the projection ────────────────────────────────────────────────────────


class _Cur:
    def __init__(self, mart_dates: list[date], off: list[date] | None = None) -> None:
        self.mart_dates = mart_dates
        self.off = off or []
        self.executed: list[tuple[str, Any]] = []
        self.rowcount = 0
        self._last = ""

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        self._last = sql
        if "INSERT INTO" in sql:
            self.rowcount = 3

    def fetchone(self) -> Any:
        if "pg_try_advisory_lock" in self._last:
            return (True,)
        if "max(trade_date)" in self._last:
            return (max(self.mart_dates) if self.mart_dates else None,)
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        if "DISTINCT trade_date FROM dw_stock.mart_sepa_feature_daily" in self._last:
            return [(d,) for d in self.mart_dates]
        if "WITH stamped" in self._last:
            return [(d,) for d in self.off]
        return []

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, cur: _Cur) -> None:
        self.cur = cur
        self.commits = 0

    def cursor(self) -> _Cur:
        return self.cur

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def _inserts(cur: _Cur) -> list[tuple[str, Any]]:
    return [(sql, params) for sql, params in cur.executed if "INSERT INTO" in sql]


def test_projection_writes_the_session_it_is_given() -> None:
    cur = _Cur([MON])
    result = sp.run_sepa_projection(_Conn(cur), trade_date=MON)
    assert result["trade_date"] == "2026-10-05" and result["rows_written"] == 3
    (sql, params), = _inserts(cur)
    assert "WHERE m.trade_date = %s" in sql and params[2] == MON


def test_a_mart_stamped_with_another_day_is_not_written() -> None:
    # The 2026-10-06 state: the mart said Tuesday for Monday's bars.
    cur = _Cur([TUE])
    result = sp.run_sepa_projection(_Conn(cur), trade_date=MON)
    assert result["skipped"] is True and result["rows_written"] == 0
    assert result["mart_trade_date"] == "2026-10-06"
    assert "not the session 2026-10-05" in result["reason"]
    assert _inserts(cur) == []


def test_a_mart_holding_two_sessions_is_not_written() -> None:
    cur = _Cur([FRI, MON])
    result = sp.run_sepa_projection(_Conn(cur), trade_date=MON)
    assert result["skipped"] is True and _inserts(cur) == []


def test_without_a_session_the_mart_session_is_used() -> None:
    cur = _Cur([MON])
    assert sp.run_sepa_projection(_Conn(cur))["trade_date"] == "2026-10-05"


def test_an_empty_mart_is_skipped() -> None:
    result = sp.run_sepa_projection(_Conn(_Cur([])), trade_date=MON)
    assert result["skipped"] is True and result["reason"] == "mart_sepa_feature_daily empty"


# ── the asset check (ratchet) ─────────────────────────────────────────────


def test_off_session_sql_flags_weekends_holidays_and_future_dates() -> None:
    sql = sp.OFF_SESSION_DATES_SQL
    assert "features.stock_signal_sepa_daily" in sql
    assert "isodow" in sql and "> 5" in sql
    assert "us_market_holiday" in sql and "status = 'closed'" in sql
    assert "symbol = 'SPY'" in sql


def _check(monkeypatch: pytest.MonkeyPatch, *, off: list[date], newest: date, session: date) -> Any:
    pytest.importorskip("dagster")
    from bifrost_research.orchestration import sepa_projection_asset as spa

    cur = _Cur([newest], off=off)
    monkeypatch.setattr("bifrost_research.db.conn.connect", lambda: _Conn(cur))
    monkeypatch.setattr("bifrost_research.db.calendar.latest_closed_session", lambda _c, **_k: session)
    return spa.sepa_sessions_are_trading_days()


def test_check_passes_on_session_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _check(monkeypatch, off=[], newest=MON, session=MON)
    assert result.passed is True
    assert result.metadata["off_session_dates"].value == 0


def test_check_fails_on_a_saturday_row(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _check(monkeypatch, off=[SAT], newest=MON, session=MON)
    assert result.passed is False
    assert result.metadata["off_session_sample"].value == "2026-10-03"


def test_check_fails_when_the_newest_row_is_not_the_new_york_session(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _check(monkeypatch, off=[], newest=TUE, session=MON)
    assert result.passed is False
