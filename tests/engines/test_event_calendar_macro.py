"""Ratchet (TD-181): the calendar's macro rows are exactly features.macro_event_daily.

/events/calendar took its macro rows from a hand-dropped radar file
(``ws:macro-calendar-2026q4``, last date 2026-12-10) whose ids hash the collection
date, so a re-drop would have duplicated every release. The calendar now reads
macro rows from ``features.macro_event_daily`` for its window and skips radar
rows from ``ws:macro*`` sources; the file ingest archives such files unread.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bifrost_research.engines.event_radar import event_calendar as ec
from bifrost_research.engines.event_radar.ingest import ingest_directory
from bifrost_research.scheduler import macro_ingest as mi

TODAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc)

# The radar's own rows: one dividend date (kept) and two rows from the retired
# macro file (skipped). Values are made up.
DIVIDEND = {
    "event_id": "WSCORP-20260924-007-aaaaaa",
    "batch_id": "file-test",
    "collected_at": date(2026, 9, 24),
    "source": "ws:corporate-actions-test",
    "subject": "2026-10-30 XYZ d",
    "event_summary": "2026-10-30 XYZ dividend payment ahead",
    "affected_symbols": "XYZ",
    "direction": 0,
    "certainty": 0,
    "sentiment": 0,
    "theme": "",
    "importance": 2,
    "event_date": date(2026, 10, 30),
    "date_basis": "2026-10-30",
    "computed_at": NOW,
}
RETIRED_MACRO = [
    {**DIVIDEND, "event_id": f"WSMACR-2026092{i}-001-bbbbbb", "source": "ws:macro-calendar-2026q4",
     "affected_symbols": "", "event_date": date(2026, 10, 14), "subject": "2026-10-14 CPI r"}
    for i in (4, 5)
]


def _macro_table() -> list[dict[str, Any]]:
    """The packaged seed as macro_event_daily holds it, plus one past drop row."""
    rows = [
        dict(zip(mi._MACRO_COLS, r))
        for r in mi.parse_macro_csv(mi.seed_text(), source=mi.SEED_SOURCE, now=NOW, default_forward=True)
    ]
    drop = mi.parse_macro_csv(
        "event_date,country,indicator,actual,expected,prior\n2026-09-11,US,CPI,2.9,3.0,2.7\n",
        source=mi.DROP_SOURCE,
        now=NOW,
    )
    rows.extend(dict(zip(mi._MACRO_COLS, r)) for r in drop)
    return rows


class _Cur:
    """Answers the three calendar reads; applies the macro window like Postgres would."""

    def __init__(self, macro: list[dict[str, Any]], radar: list[dict[str, Any]]) -> None:
        self.macro = macro
        self.radar = radar
        self.executed: list[tuple[str, Any]] = []
        self._last = ""
        self._params: Any = None

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._last = " ".join(sql.split())
        self._params = params
        self.executed.append((self._last, params))

    def fetchall(self) -> list[dict[str, Any]]:
        if f"FROM {ec.MACRO_TABLE}" in self._last:
            since = self._params[0]
            rows = [r for r in self.macro if r["event_date"] >= since]
            return [{c: r.get(c) for c in ec.MACRO_COLS} for r in sorted(rows, key=lambda r: r["event_date"])]
        if f"FROM {ec.RADAR_TABLE}" in self._last:
            # Return the macro-file rows too: the Python rule must hold even if
            # the SQL filter were lost.
            return [dict(r) for r in self.radar]
        raise AssertionError(self._last)

    def fetchone(self) -> dict[str, Any]:
        superseded = sum(1 for r in self.radar if ec.is_superseded_macro_source(r["source"]))
        return {"placeholder_rows": 0, "superseded_rows": superseded}


class _Conn:
    def __init__(self, cur: _Cur) -> None:
        self.cur = cur

    def cursor(self) -> _Cur:
        return self.cur


def _read(limit: int = 500) -> tuple[dict[str, Any], _Cur, list[dict[str, Any]]]:
    macro = _macro_table()
    cur = _Cur(macro, [DIVIDEND, *RETIRED_MACRO])
    return ec.read_event_calendar(_Conn(cur), limit=limit, today=TODAY), cur, macro


def test_calendar_macro_rows_equal_macro_event_daily_for_the_window() -> None:
    body, _cur, macro = _read()
    since = TODAY - timedelta(days=ec.MACRO_LOOKBACK_DAYS)
    expected = sorted(r["macro_id"] for r in macro if r["event_date"] >= since)
    got = [r for r in body["rows"] if r["origin"] == ec.ORIGIN_MACRO]
    assert sorted(r["event_id"] for r in got) == expected
    assert len(expected) >= 13  # the seed reaches 2027-12; the window must not cut the future
    by_id = {r["macro_id"]: r for r in macro}
    for row in got:
        assert row["event_date"] == by_id[row["event_id"]]["event_date"].isoformat()
    assert body["macro_rows"] == len(expected)
    assert body["macro_window_start"] == since.isoformat()
    # The past drop row (09-11) is inside the window; January's FOMC is not.
    assert "macro-us-cpi-2026-09-11" in {r["event_id"] for r in got}
    assert not any(r["event_date"] < since.isoformat() for r in got)


def test_retired_radar_macro_rows_are_skipped() -> None:
    body, cur, _macro = _read()
    assert not any(ec.is_superseded_macro_source(r.get("source")) for r in body["rows"])
    assert body["superseded_macro_rows"] == 2
    radar_sql = next(sql for sql, _ in cur.executed if f"FROM {ec.RADAR_TABLE}" in sql and "LIMIT" in sql)
    assert f"NOT {ec.SUPERSEDED_MACRO_SQL}" in radar_sql
    # The dividend date is still the radar's.
    assert [r["event_id"] for r in body["rows"] if r["origin"] == ec.ORIGIN_RADAR] == [DIVIDEND["event_id"]]


def test_response_keeps_the_radar_shape() -> None:
    body, _cur, _macro = _read()
    assert {"rows", "count", "excluded_placeholder_rows"} <= set(body)
    assert body["count"] == len(body["rows"])
    for row in body["rows"]:
        assert set(ec.RADAR_COLS) <= set(row), row
    dates = [r["event_date"] for r in body["rows"]]
    assert dates == sorted(dates)


def test_macro_rows_read_as_the_frontend_expects() -> None:
    body, _cur, _macro = _read()
    macro = {r["event_id"]: r for r in body["rows"] if r["origin"] == ec.ORIGIN_MACRO}
    cpi = macro["macro-us-cpi-2026-10-14"]
    fomc = macro["macro-us-fomc-rate-decision-2026-10-28"]
    # eventsLayer.macroItems: no symbol, cell = first word after the date.
    for row, cell in ((cpi, "CPI"), (fomc, "FOMC")):
        assert row["affected_symbols"] == ""
        assert row["event_summary"].split()[1] == cell
        assert row["subject"].startswith(row["event_date"])
        assert row["theme"] == "利率路径重定价"
    assert fomc["importance"] > cpi["importance"] >= 1


def test_limit_applies_to_the_merged_calendar() -> None:
    body, _cur, _macro = _read(limit=3)
    assert body["count"] == 3


def test_ingest_archives_a_macro_file_unread(tmp_path: Path) -> None:
    inbox = tmp_path / "input"
    inbox.mkdir()
    (inbox / "macro-calendar-2027q1.md").write_text(
        "- 2027-01-13 CPI release for December 2026\n", encoding="utf-8"
    )
    archive = tmp_path / "archive"
    summary = ingest_directory(inbox, archive_dir=archive, upsert=True, archive=True, conn=object())
    (result,) = summary.results
    assert result.skipped and result.rows_written == 0 and result.archived
    assert result.skip_reason.startswith("macro_calendar_superseded")
    assert summary.files_skipped == 1 and summary.rows_written == 0
    assert not list(inbox.iterdir())
