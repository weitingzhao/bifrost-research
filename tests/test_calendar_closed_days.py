"""fetch_closed_holiday_dates: the table it read never existed (2026-09-26)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

from bifrost_research.db import calendar as cal


class _Cursor:
    def __init__(self, answers):
        self.answers = answers
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def execute(self, sql, params=None):
        key = "holiday" if "us_market_holiday" in sql else "bars"
        self.rows = self.answers[key](params)

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, answers):
        self.c = _Cursor(answers)

    def cursor(self):
        return self.c

    def rollback(self):
        return None


class _FakeNow:
    @staticmethod
    def now(tz=None):
        return datetime(2026, 9, 26, 12, 0, tzinfo=tz)


def _bars_every_weekday_except(*skip: date):
    def answer(params):
        start, end = params
        out, d = [], start
        while d <= end:
            if d.weekday() < 5 and d not in skip:
                out.append((d,))
            d += timedelta(days=1)
        return out

    return answer


def test_a_past_weekday_without_etf_bars_is_closed_and_the_feed_covers_the_future() -> None:
    conn = _Conn(
        {
            "holiday": lambda p: [(date(2026, 11, 26),)],
            "bars": _bars_every_weekday_except(date(2026, 9, 7)),
        }
    )
    with patch.object(cal, "datetime", _FakeNow):
        closed = cal.fetch_closed_holiday_dates(conn, start=date(2026, 9, 1), end=date(2026, 11, 30))
    assert date(2026, 9, 7) in closed        # Labor Day, known only from the missing bars
    assert date(2026, 11, 26) in closed      # Thanksgiving, known only from the feed
    assert date(2026, 9, 8) not in closed
    # today and later are never inferred from missing bars (today's bar lands after the close)
    assert date(2026, 9, 28) not in closed


def test_an_unreadable_bar_source_closes_nothing() -> None:
    conn = _Conn({"holiday": lambda p: [], "bars": lambda p: []})
    with patch.object(cal, "datetime", _FakeNow):
        closed = cal.fetch_closed_holiday_dates(conn, start=date(2026, 9, 1), end=date(2026, 9, 25))
    assert closed == set()


def test_trading_days_step_over_the_holiday() -> None:
    conn = _Conn({"holiday": lambda p: [], "bars": _bars_every_weekday_except(date(2026, 9, 7))})
    with patch.object(cal, "datetime", _FakeNow):
        days = cal.fetch_recent_trading_days(conn, 1, as_of=date(2026, 9, 7))
    assert days == [date(2026, 9, 4)]
