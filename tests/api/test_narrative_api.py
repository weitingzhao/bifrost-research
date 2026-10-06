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

    def __init__(
        self,
        calls: list[tuple[str, tuple[Any, ...]]],
        coverage: tuple[Any, ...],
        item_202: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self.calls = calls
        self.coverage = coverage
        self.item_202 = item_202 or []
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
        elif flat.startswith("SELECT filing_date, items_text"):
            self._rows = [(FILING[2], FILING[4]), *self.item_202] if FILING[1] in params else []
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
    def __init__(self, coverage: tuple[Any, ...] = (0, None, None), item_202: list[tuple[Any, ...]] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.coverage = coverage
        self.item_202 = item_202 or []

    def cursor(self) -> _Cur:
        return _Cur(self.calls, self.coverage, self.item_202)

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


def test_a_202_filing_that_is_not_a_results_release_is_set_aside(monkeypatch) -> None:
    # Invented: a delivery report under Item 2.02, a week before the name's results release.
    head = "Item 2.02 Results of Operations and Financial Condition. "
    deliveries = (date(2031, 2, 18), head + "Zeta Corp published the press release attached as Exhibit 99.1.")
    release = (date(2031, 2, 25), head + "Zeta Corp released its results for the quarter ended December 31, 2030.")
    conn = _Conn(coverage=(14, date(2029, 9, 3), date(2031, 3, 11)), item_202=[deliveries, release])
    body = _client(monkeypatch, conn).get("/research/narrative/earnings?symbol=ZZZ").json()["data"]
    assert body["dates"] == ["2031-02-25", "2031-03-11"]
    assert [(a["filed"], a["release"]) for a in body["set_aside"]] == [("2031-02-18", "2031-02-25")]
    # Two prints are not a cadence: nothing is estimated.
    assert body["expected_next"] is None


def test_earnings_for_a_name_the_feed_never_carried_says_so(monkeypatch) -> None:
    body = _client(monkeypatch, _Conn()).get("/research/narrative/earnings?symbol=QQQQ").json()["data"]
    assert body["dates"] == [] and body["filings"] == 0


# --- TD-158: the batch answers each name exactly as the single route does -------------

_HEAD = "Item 2.02 Results of Operations and Financial Condition. "
# Invented filings: (symbol, filing_date, items, items_text).
_FEED = [
    # ZZA: five quarterly results releases — enough cadence for an estimate.
    *[
        ("ZZA", d, ["2.02", "9.01"], _HEAD + "Zeta A released its results for the quarter.")
        for d in (date(2030, 2, 25), date(2030, 5, 20), date(2030, 8, 19), date(2030, 11, 18), date(2031, 2, 24))
    ],
    ("ZZA", date(2030, 6, 2), ["5.07"], "Item 5.07 Submission of Matters to a Vote of Security Holders."),
    # ZZB: a delivery report under 2.02 a week before the release — set aside.
    ("ZZB", date(2031, 2, 18), ["2.02"], _HEAD + "Zeta B published the press release attached as Exhibit 99.1."),
    ("ZZB", date(2031, 2, 25), ["2.02"], _HEAD + "Zeta B released its results for the quarter ended December 31, 2030."),
    # ZZC: 8-Ks on file, none of them a 2.02.
    ("ZZC", date(2031, 1, 5), ["8.01"], "Item 8.01 Other Events."),
]


class _FeedCur:
    """Answers the single route's two reads and the batch's one from ``_FEED``."""

    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.calls = calls
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        flat = " ".join(sql.split())
        self.calls.append((flat, tuple(params)))
        if flat.startswith("SELECT COUNT(*), MIN(filing_date), MAX(filing_date)"):
            mine = [f for f in _FEED if f[0] == params[0]]
            days = [f[1] for f in mine]
            self._rows = [(len(mine), min(days) if days else None, max(days) if days else None)]
        elif flat.startswith("SELECT filing_date, items_text"):
            self._rows = [(f[1], f[3]) for f in _FEED if f[0] == params[0] and "2.02" in f[2]]
        elif flat.startswith("SELECT symbol, COUNT(*)"):
            self._rows = []
            for sym in dict.fromkeys(f[0] for f in _FEED if f[0] in params[0]):
                mine = [f for f in _FEED if f[0] == sym]
                r202 = [f for f in mine if "2.02" in f[2]]
                self._rows.append(
                    (
                        sym,
                        len(mine),
                        min(f[1] for f in mine),
                        max(f[1] for f in mine),
                        [f[1] for f in r202],
                        [f[3] for f in r202],
                    )
                )
        else:
            self._rows = []

    def fetchone(self) -> tuple[Any, ...]:
        return self._rows[0]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FeedConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self) -> _FeedCur:
        return _FeedCur(self.calls)

    def close(self) -> None:
        return None


def test_the_batch_reads_each_name_as_the_single_route_does(monkeypatch) -> None:
    monkeypatch.setattr(narrative_api, "ny_today", lambda: date(2031, 3, 1))
    names = ["ZZA", "ZZB", "ZZC", "QQQQ"]
    conn = _FeedConn()
    client = _client(monkeypatch, conn)  # type: ignore[arg-type]
    single = {s: client.get(f"/research/narrative/earnings?symbol={s}").json()["data"] for s in names}
    conn.calls.clear()
    batch = client.get("/research/narrative/earnings/batch?symbols=zza, ZZB,ZZC,QQQQ,zza").json()["data"]
    assert list(batch) == names
    assert batch == single
    # The fixture exercises every branch: an estimate, a set-aside filing, 8-Ks with no
    # 2.02, and a name the feed never carried.
    assert batch["ZZA"]["expected_next"] is not None
    assert batch["ZZB"]["set_aside"] and batch["ZZB"]["expected_next"] is None
    assert batch["ZZC"]["filings"] == 1 and batch["ZZC"]["dates"] == []
    assert batch["QQQQ"]["filings"] == 0 and batch["QQQQ"]["first_filed"] is None
    # One statement, names passed as a parameter, never normalised on the column.
    assert len(conn.calls) == 1
    assert conn.calls[0][1] == (names,)
    assert "UPPER(" not in conn.calls[0][0]


def test_the_batch_refuses_none_too_many_or_a_long_name(monkeypatch) -> None:
    client = _client(monkeypatch, _FeedConn())  # type: ignore[arg-type]
    assert client.get("/research/narrative/earnings/batch?symbols=,%20,").status_code == 422
    too_many = ",".join(f"N{i}" for i in range(narrative_api.EARNINGS_BATCH_MAX + 1))
    assert client.get(f"/research/narrative/earnings/batch?symbols={too_many}").status_code == 422
    assert client.get("/research/narrative/earnings/batch?symbols=NVDA," + "X" * 17).status_code == 422
    at_cap = ",".join(f"N{i}" for i in range(narrative_api.EARNINGS_BATCH_MAX))
    assert client.get(f"/research/narrative/earnings/batch?symbols={at_cap}").status_code == 200
