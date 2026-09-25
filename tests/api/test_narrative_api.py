"""GET /research/narrative — the one-name read. Fixtures are invented."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Self

from fastapi.testclient import TestClient

from bifrost_research.api import narrative as narrative_api
from bifrost_research.api.app import create_app

FILING = (
    "0000000000-31-000001",
    "ZZZ",
    date(2031, 3, 11),
    ["2.02", "9.01"],
    "Item 2.02 Results of Operations and Financial Condition. Zeta Corp issued a press release.",
    "https://www.sec.gov/Archives/edgar/data/1/0000000000-31-000001.txt",
)


class _Cur:
    """Answers each statement by what it reads, and keeps what it was asked."""

    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]], coverage: tuple[Any, ...]) -> None:
        self.calls = calls
        self.coverage = coverage
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.calls.append((" ".join(sql.split()), tuple(params)))
        flat = " ".join(sql.split())
        if flat.startswith("SELECT COUNT(*), COUNT(DISTINCT symbol)"):
            self._rows = [(14, 3, date(2029, 9, 3), date(2031, 3, 11), datetime(2031, 3, 12, tzinfo=UTC))]
        elif flat.startswith("SELECT COUNT(*) FROM ("):
            self._rows = [(10,)]
        elif "FROM raw_market.sec_10k_section" in flat:
            self._rows = []
        elif flat.startswith("SELECT COUNT(*), MIN(filing_date), MAX(filing_date)"):
            self._rows = [self.coverage]
        elif flat.startswith("SELECT DISTINCT filing_date"):
            self._rows = [(FILING[2],)] if FILING[1] in params else []
        elif "FROM raw_market.sec_8k_filing" in flat and "items_text" in flat:
            # The filing is ZZZ's: a read narrowed to another name does not see it.
            self._rows = [FILING] if "symbol = %s" not in flat or FILING[1] in params else []
        else:
            self._rows = []

    def fetchone(self) -> tuple[Any, ...]:
        return self._rows[0]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _Conn:
    def __init__(self, coverage: tuple[Any, ...] = (0, None, None)) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.coverage = coverage

    def cursor(self) -> _Cur:
        return _Cur(self.calls, self.coverage)

    def close(self) -> None:
        return None


def _client(monkeypatch, conn: _Conn) -> TestClient:
    monkeypatch.setattr("bifrost_research.api.health.run_startup_schema_guard", lambda: None)
    monkeypatch.setattr(narrative_api, "_connect_or_503", lambda: conn)
    return TestClient(create_app())


def test_without_a_symbol_the_window_is_every_name_and_there_is_no_coverage(monkeypatch) -> None:
    conn = _Conn()
    body = _client(monkeypatch, conn).get("/research/narrative?days=7").json()["data"]
    assert body["symbol_coverage"] is None
    assert not any("symbol = %s" in sql for sql, _ in conn.calls)


def test_a_symbol_narrows_the_window_and_says_how_much_of_that_name_is_on_file(monkeypatch) -> None:
    conn = _Conn(coverage=(7, date(2029, 10, 1), date(2031, 3, 11)))
    body = _client(monkeypatch, conn).get("/research/narrative?days=7&symbol=%20zzz%20").json()["data"]
    assert body["symbol_coverage"] == {
        "symbol": "ZZZ",
        "filings": 7,
        "first_filed": "2029-10-01",
        "last_filed": "2031-03-11",
    }
    narrowed = [(sql, args) for sql, args in conn.calls if "symbol = %s" in sql]
    # Coverage, the filings and the vendor labels: all three read the one name,
    # passed as a parameter and normalised on the way in, never on the column.
    assert len(narrowed) == 3
    assert all("ZZZ" in args for _, args in narrowed)
    assert not any("UPPER(" in sql for sql, _ in conn.calls)
    assert [t["item"] for t in body["tags"] if t["basis"] == "sec"] == ["2.02"]


def test_a_name_the_feed_never_carried_reads_zero_not_missing(monkeypatch) -> None:
    body = _client(monkeypatch, _Conn()).get("/research/narrative?symbol=QQQQ").json()["data"]
    assert body["symbol_coverage"] == {"symbol": "QQQQ", "filings": 0, "first_filed": None, "last_filed": None}
    assert body["tags"] == []



def test_earnings_dates_are_the_names_item_202_filings(monkeypatch) -> None:
    conn = _Conn(coverage=(14, date(2029, 9, 3), date(2031, 3, 11)))
    body = _client(monkeypatch, conn).get("/research/narrative/earnings?symbol=%20zzz").json()["data"]
    assert body["symbol"] == "ZZZ"
    assert body["dates"] == ["2031-03-11"]
    assert body["filings"] == 14
    assert any("'2.02' = ANY(items)" in sql for sql, _ in conn.calls)


def test_earnings_for_a_name_the_feed_never_carried_says_so(monkeypatch) -> None:
    body = _client(monkeypatch, _Conn()).get("/research/narrative/earnings?symbol=QQQQ").json()["data"]
    assert body["dates"] == [] and body["filings"] == 0
