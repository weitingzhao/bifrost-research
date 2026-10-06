"""db/calendar.py: a failed read raises instead of answering (TD-93); one New York clock (TD-98)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from bifrost_research.db import calendar as cal

FRI, SAT, MON, TUE = date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 5), date(2026, 10, 6)
LABOR_DAY = date(2026, 9, 7)


class _PgError(Exception):
    def __init__(self, pgcode: str, msg: str = "boom") -> None:
        super().__init__(msg)
        self.pgcode = pgcode


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)
        for needle, answer in self.conn.answers.items():
            if needle in sql:
                if isinstance(answer, Exception):
                    raise answer
                self.rows = answer
                return
        self.rows = []

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.sql: list[str] = []
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self)

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture(autouse=True)
def _clear_cache():
    cal._CLOSED_CACHE.clear()
    yield
    cal._CLOSED_CACHE.clear()


# ── TD-93: the holiday read ───────────────────────────────────────────────


def test_a_failed_holiday_read_raises_rather_than_making_the_holiday_a_session() -> None:
    conn = _Conn({"us_market_holiday": _PgError("42501", "permission denied")})
    with pytest.raises(cal.CalendarUnavailable):
        cal.fetch_recent_trading_days(conn, 5, as_of=date(2026, 11, 27))
    assert conn.rollbacks == 1


def test_a_failed_bar_read_raises_too() -> None:
    conn = _Conn({"us_market_holiday": [], "stock_daily": _PgError("57014", "canceling statement")})
    with pytest.raises(cal.CalendarUnavailable):
        cal.fetch_closed_holiday_dates(conn, start=date(2026, 9, 1), end=date(2026, 9, 10))


def test_the_session_stampers_fail_on_an_unreadable_calendar() -> None:
    conn = _Conn({"us_market_holiday": _PgError("57014")})
    now = datetime(2026, 9, 8, 2, 30, tzinfo=UTC)
    with pytest.raises(cal.CalendarUnavailable):
        cal.latest_closed_session(conn, now=now)
    with pytest.raises(cal.CalendarUnavailable):
        cal.session_today(conn, now=now)


def test_the_tolerant_reader_degrades_to_weekends_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    conn = _Conn({"us_market_holiday": _PgError("57014")})
    with caplog.at_level(logging.WARNING, logger=cal.__name__):
        days = cal.cached_closed_days(conn, date(2026, 9, 1), date(2026, 9, 10))
    assert days == frozenset()
    assert "weekends only" in caplog.text
    assert cal._CLOSED_CACHE == {}  # a degraded answer is not cached


# ── TD-98: the New York clock ─────────────────────────────────────────────


def test_ny_today_is_new_yorks_date_at_the_nightly_batch() -> None:
    # 02:30 UTC Tuesday is 22:30 Monday in New York — the UTC date is a day ahead.
    assert cal.ny_today(datetime(2026, 10, 6, 2, 30, tzinfo=UTC)) == MON
    assert cal.ny_now(datetime(2026, 10, 6, 2, 30, tzinfo=UTC)).hour == 22


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError):
        cal.ny_now(datetime(2026, 10, 6, 2, 30))  # noqa: DTZ001 — the point of the test


@pytest.mark.parametrize(
    ("now", "session"),
    [
        (datetime(2026, 10, 6, 2, 30, tzinfo=UTC), MON),   # Monday night's batch
        (datetime(2026, 10, 3, 2, 30, tzinfo=UTC), FRI),   # Friday night's batch, Saturday UTC
        (datetime(2026, 10, 3, 18, 0, tzinfo=UTC), FRI),   # Saturday afternoon
        (datetime(2026, 10, 6, 14, 0, tzinfo=UTC), TUE),   # a session day before the close
    ],
)
def test_session_today_is_the_newest_session_on_or_before_new_yorks_date(
    monkeypatch: pytest.MonkeyPatch, now: datetime, session: date
) -> None:
    monkeypatch.setattr(cal, "closed_days", lambda _c, _s, _e: frozenset())
    assert cal.session_today(object(), now=now) == session


def test_session_today_steps_over_a_holiday(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cal, "closed_days", lambda _c, _s, _e: frozenset({LABOR_DAY}))
    assert cal.session_today(object(), now=datetime(2026, 9, 7, 18, 0, tzinfo=UTC)) == date(2026, 9, 4)


def test_option_universe_stamps_the_session_not_the_utc_date(monkeypatch: pytest.MonkeyPatch) -> None:
    from bifrost_research.engines.option_universe import entry as ou

    class _C:
        def cursor(self):
            return _Cursor(_Conn({}))

        def close(self):
            return None

    seen: dict[str, date] = {}
    monkeypatch.setattr(ou, "connect", lambda: _C())
    monkeypatch.setattr(ou, "session_today", lambda _conn: FRI)
    monkeypatch.setattr(ou, "load_existing", lambda _c: {})
    monkeypatch.setattr(ou, "load_resident", lambda _c: {"SPY": "benchmark"})
    monkeypatch.setattr(ou, "load_liquidity", lambda _c, day: seen.setdefault("liquidity", day) and {})
    monkeypatch.setattr(ou, "load_edge_candidates", lambda _c: set())
    monkeypatch.setattr(ou, "load_recent_bars", lambda _c, day: set())
    monkeypatch.setattr(ou, "write_universe", lambda _c, rows, existing: {"written": len(rows), "removed": 0})
    out = ou.run()
    assert out["as_of"] == FRI.isoformat()
    assert seen["liquidity"] == FRI


# ── TD-93: the universe ───────────────────────────────────────────────────


@pytest.fixture
def no_watchlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RESEARCH_WATCHLIST", raising=False)


@pytest.mark.parametrize("pgcode", ["42501", "57014"])  # InsufficientPrivilege, QueryCanceled
def test_a_failed_universe_read_raises_instead_of_swapping_the_universe(no_watchlist: None, pgcode: str) -> None:
    conn = _Conn(
        {
            "research.option_universe": _PgError(pgcode),
            "option_open_interest": [("AAPL",), ("ZZZ",)],
        }
    )
    with pytest.raises(_PgError):
        cal.load_symbols_from_env_or_query(conn)
    assert not any("option_open_interest" in s for s in conn.sql)


def test_an_absent_universe_table_falls_back_to_open_interest(no_watchlist: None) -> None:
    conn = _Conn({"research.option_universe": _PgError("42P01"), "option_open_interest": [("aapl",), ("MSFT",)]})
    assert cal.load_symbols_from_env_or_query(conn) == ["AAPL", "MSFT"]


def test_the_rule_wins_when_it_has_rows(no_watchlist: None) -> None:
    conn = _Conn({"research.option_universe": [("NVDA",)], "option_open_interest": [("AAPL",)]})
    assert cal.load_symbols_from_env_or_query(conn) == ["NVDA"]


def test_no_universe_anywhere_raises(no_watchlist: None) -> None:
    conn = _Conn({"research.option_universe": [], "option_open_interest": []})
    with pytest.raises(cal.UniverseUnavailable):
        cal.load_symbols_from_env_or_query(conn)


def test_a_failed_fallback_read_raises(no_watchlist: None) -> None:
    conn = _Conn({"research.option_universe": [], "option_open_interest": _PgError("57014")})
    with pytest.raises(_PgError):
        cal.load_symbols_from_env_or_query(conn)


# ── ratchet: one place resolves "today" ───────────────────────────────────

_SRC = Path(__file__).resolve().parents[1] / "src" / "bifrost_research"
_PRIVATE_TODAY = re.compile(r"^\s*def _\w*(?:today|_now|now_)\w*\s*\(", re.M)
#: Not session clocks: the Copilot usage day is UTC by contract (``day_utc``),
#: and ``_is_today`` compares against it.
_ALLOWED = {"copilot/standing.py"}


def test_no_private_today_helper_outside_db_calendar() -> None:
    hits = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel == "db/calendar.py" or rel in _ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for m in _PRIVATE_TODAY.finditer(text):
            hits.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
    assert not hits, (
        "A private 'today' helper — use db/calendar.ny_today / session_today / "
        "latest_closed_session (TD-98):\n" + "\n".join(hits)
    )
