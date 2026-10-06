"""SEC 8-K -> Event Radar in Dagster (TD-100): idempotent, no watermark file."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from bifrost_research.engines.event_radar import sec_source
from bifrost_research.engines.event_radar.sec_source import (
    filing_head,
    head_of_line,
    one_line,
    select_new_lines,
)

FETCHED = datetime(2026, 10, 6, 4, 30, tzinfo=timezone.utc)


def _row(symbol: str, items: list[str], *, category: str | None = None, body: str = "") -> tuple:
    return (symbol, date(2026, 10, 5), items, body, FETCHED, category, None)


def test_line_is_the_one_the_mac_loop_wrote() -> None:
    """Same text as scripts/event_radar_sec_source.py produced, so its 17k rows dedupe."""
    row = _row("ACME", ["2.02", "9.01"], category="earnings", body="Line one\n\n  line   two")
    assert one_line(row) == (
        "2026-10-05 ACME filed an 8-K (items 2.02, 9.01) — a results announcement "
        "— classified earnings : Line one line two. reported via the SEC filings feed."
    )
    assert "\n" not in one_line(_row("ACME", ["8.01"], body="a\n" * 500))


def test_head_round_trips_through_the_line() -> None:
    for row in (_row("ACME", ["2.02"]), _row("BRK.B", []), _row("ZZ", ["5.02", "9.01"])):
        assert head_of_line(one_line(row)) == filing_head(row[0], row[1], row[2])
    assert head_of_line("Fed holds rates steady") is None


def test_already_ingested_filings_are_skipped() -> None:
    rows = [_row("ACME", ["2.02"]), _row("BETA", ["8.01"])]
    existing = [one_line(rows[0])]
    assert select_new_lines(rows, existing) == [one_line(rows[1])]
    assert select_new_lines(rows, [one_line(r) for r in rows]) == []


def test_a_later_classification_does_not_make_a_filing_new() -> None:
    """The Mac wrote the line before the vendor classified the filing."""
    plain = _row("ACME", ["2.02"])
    classified = _row("ACME", ["2.02"], category="earnings")
    assert select_new_lines([classified], [one_line(plain)]) == []


def test_filings_sharing_a_head_each_get_one_line() -> None:
    """An amendment repeats symbol, date and items; it still needs its own row."""
    first, amendment = _row("ACME", ["5.02"]), _row("ACME", ["5.02"])
    assert len(select_new_lines([first, amendment], [one_line(first)])) == 1
    assert len(select_new_lines([first, amendment], [])) == 2


def test_limit_caps_one_run() -> None:
    rows = [_row(f"S{i}", ["8.01"]) for i in range(10)]
    assert len(select_new_lines(rows, [], limit=4)) == 4


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn
        self._rows: list[Any] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        if "raw_market.sec_8k_filing" in sql:
            self._rows = self.conn.filings
        else:
            self.conn.existing_since = params["since"]
            self._rows = [(t,) for t in self.conn.existing]

    def fetchall(self) -> list[Any]:
        return self._rows


class _Conn:
    def __init__(self, filings: list[tuple], existing: list[str]) -> None:
        self.filings = filings
        self.existing = existing
        self.existing_since: date | None = None

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def rollback(self) -> None:
        return None


def test_ingest_upserts_only_new_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_row("ACME", ["2.02"]), _row("BETA", ["8.01"])]
    conn = _Conn(rows, [one_line(rows[0])])
    written: list[Any] = []
    monkeypatch.setattr(
        "bifrost_research.engines.event_radar.pipeline.upsert_events",
        lambda c, result: written.append(result) or len(result.kept) + len(result.dropped),
    )
    now = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)
    out = sec_source.ingest_sec_filings(conn, now=now)
    assert out.filings_read == 2
    assert out.lines_new == 1
    assert out.source == "ws:sec-8k-20261006T050000Z"
    assert len(written) == 1
    assert [t.raw.raw_text for t in written[0].kept + written[0].dropped] == [one_line(rows[1])]
    # Central-time calendar, as the Mac loop wrote it: 05:00 UTC is still 10-06 00:00 CDT.
    events = written[0].kept + written[0].dropped
    assert {t.raw.collected_at for t in events} == {date(2026, 10, 6)}
    assert conn.existing_since == date(2026, 9, 27)


def test_ingest_with_nothing_new_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_row("ACME", ["2.02"])]
    conn = _Conn(rows, [one_line(rows[0])])
    monkeypatch.setattr(
        "bifrost_research.engines.event_radar.pipeline.upsert_events",
        lambda *a, **k: pytest.fail("must not upsert"),
    )
    out = sec_source.ingest_sec_filings(conn, now=datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc))
    assert (out.lines_new, out.rows_written, out.source) == (0, 0, None)


def test_a_database_error_raises() -> None:
    class _Broken(_Conn):
        def cursor(self) -> _Cursor:
            raise RuntimeError("password authentication failed for user")

    with pytest.raises(RuntimeError):
        sec_source.ingest_sec_filings(_Broken([], []))
