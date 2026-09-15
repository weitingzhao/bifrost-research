"""Pinned option contracts — IB key mapping, pin_until, and the skip on a down Trade API (R9 F5)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from bifrost_research.engines.option_pinned import entry as pinned

TODAY = date(2026, 9, 15)


def _exec_row(key: str, traded: str, sec_type: str = "OPT") -> dict[str, Any]:
    return {"contract_key": key, "sec_type": sec_type, "trade_date": traded}


class _Cur:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, Any]] = []
        self.many: list[list[tuple[Any, ...]]] = []
        self.many_sql: list[str] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def executemany(self, sql: str, rows: Any) -> None:
        self.many_sql.append(sql)
        self.many.append(list(rows))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.cur = _Cur(rows or [])
        self.commits = 0

    def cursor(self) -> _Cur:
        return self.cur

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        pass


def test_contract_key_parsing_takes_the_root_from_the_occ_local_symbol() -> None:
    # Executions carry the OCC local symbol, position attribution the bare root;
    # both must land on the same contract.
    from_exec = pinned.parse_contract_key("NVDA  261120C00245000|OPT|20261120|245.0|C")
    from_attr = pinned.parse_contract_key("NVDA|OPT|20261120|245.0|C")
    assert from_exec == from_attr
    assert from_exec == {
        "underlying": "NVDA",
        "expiry": date(2026, 11, 20),
        "strike": 245.0,
        "option_right": "C",
    }
    # An adjusted root stays as IB wrote it; the catalog probe handles the rest.
    assert pinned.parse_contract_key("BDX1  260918C00085000|OPT|20260918|85.0|C")["underlying"] == "BDX1"
    assert pinned.canonical_root("BDX1") == "BDX" and pinned.canonical_root("BDX") is None


def test_anything_that_is_not_a_complete_option_key_is_not_guessed() -> None:
    assert pinned.parse_contract_key("NVDA|STK") is None
    assert pinned.parse_contract_key("NVDA|OPT|20261120|245.0") is None
    assert pinned.parse_contract_key("NVDA|OPT|20261120|not-a-strike|C") is None
    assert pinned.parse_contract_key("NVDA|OPT|2026-11-20|245.0|C") is None
    assert pinned.parse_contract_key("NVDA|OPT|20261120|245.0|X") is None
    assert pinned.parse_contract_key("NVDA|OPT|20261120|0|C") is None
    assert pinned.parse_contract_key(None) is None


def test_pin_until_is_expiry_or_the_tail_after_the_last_sighting() -> None:
    # A long-dated contract is pinned to its expiry.
    assert pinned.pin_until(date(2027, 6, 18), TODAY) == date(2027, 6, 18)
    # An expired one still keeps 180 days of post-mortem room.
    assert pinned.pin_until(date(2026, 3, 20), TODAY) == TODAY + timedelta(days=180)


def test_held_wins_over_closed_and_first_pinned_is_the_earliest_trade() -> None:
    held = [pinned.parse_contract_key("NVDA|OPT|20261120|245.0|C")]
    executions = [
        {**pinned.parse_contract_key("NVDA|OPT|20261120|245.0|C"), "traded_on": date(2026, 5, 4)},
        {**pinned.parse_contract_key("NVDA|OPT|20261120|245.0|C"), "traded_on": date(2026, 9, 1)},
        {**pinned.parse_contract_key("MU|OPT|20261016|180.0|P"), "traded_on": date(2026, 8, 20)},
        # Traded 200 days ago and not held — outside the window, so not pinned.
        {**pinned.parse_contract_key("AMD|OPT|20260320|150.0|C"), "traded_on": date(2026, 2, 1)},
    ]
    pins = pinned.build_pins(as_of=TODAY, held=held, executions=executions)
    nvda = pins[("NVDA", date(2026, 11, 20), 245.0, "C")]
    assert nvda["reason"] == "held"
    assert nvda["first_pinned"] == date(2026, 5, 4) and nvda["last_seen"] == TODAY
    mu = pins[("MU", date(2026, 10, 16), 180.0, "P")]
    assert mu["reason"] == "closed_recent" and mu["last_seen"] == date(2026, 8, 20)
    assert ("AMD", date(2026, 3, 20), 150.0, "C") not in pins


def test_a_held_leg_with_no_execution_on_record_is_pinned_from_today() -> None:
    held = [pinned.parse_contract_key("PLTR|OPT|20270115|200.0|C")]
    pins = pinned.build_pins(as_of=TODAY, held=held, executions=[])
    row = pins[("PLTR", date(2027, 1, 15), 200.0, "C")]
    assert row["first_pinned"] == TODAY == row["last_seen"]


def test_the_catalog_join_finds_adjusted_families_and_reports_what_it_cannot() -> None:
    keys = [("BDX1", date(2026, 9, 18), 85.0, "C"), ("NVDA", date(2026, 11, 20), 245.0, "C")]
    rows = [
        # The catalog files the adjusted contract under the canonical underlying.
        ("O:BDX1260918C00085000", "BDX", date(2026, 9, 18), 85.0, "C"),
        ("O:BDX260918C00085000", "BDX", date(2026, 9, 18), 85.0, "C"),
    ]
    conn = _Conn(rows)
    catalog = pinned.load_catalog(conn, keys)
    probed = conn.cur.executed[0][1][0]
    assert "BDX" in probed and "BDX1" in probed  # both roots are probed
    pins = {
        keys[0]: {
            "underlying": "BDX1",
            "expiry": date(2026, 9, 18),
            "strike": 85.0,
            "option_right": "C",
            "reason": "held",
            "first_pinned": TODAY,
            "last_seen": TODAY,
            "pin_until": TODAY,
        },
        keys[1]: {
            "underlying": "NVDA",
            "expiry": date(2026, 11, 20),
            "strike": 245.0,
            "option_right": "C",
            "reason": "held",
            "first_pinned": TODAY,
            "last_seen": TODAY,
            "pin_until": TODAY,
        },
    }
    written, unmatched = pinned.resolve_tickers(pins, catalog)
    # Both members of the adjusted family are pinned; losing one loses history.
    assert sorted(r["option_ticker"] for r in written) == [
        "O:BDX1260918C00085000",
        "O:BDX260918C00085000",
    ]
    assert all(r["ambiguous"] for r in written)
    # The contract the catalog does not have is listed, not invented.
    assert [u["underlying"] for u in unmatched] == ["NVDA"]
    assert unmatched[0]["expiry"] == "2026-11-20"


def test_writes_are_upserts_that_never_shorten_a_pin() -> None:
    conn = _Conn()
    rows = [
        {
            "option_ticker": "O:NVDA261120C00245000",
            "underlying": "NVDA",
            "expiry": date(2026, 11, 20),
            "strike": 245.0,
            "option_right": "C",
            "reason": "held",
            "first_pinned": date(2026, 5, 4),
            "last_seen": TODAY,
            "pin_until": date(2026, 11, 20),
            "ambiguous": False,
        }
    ]
    assert pinned.write_pins(conn, rows) == 1
    assert conn.commits == 1
    written = conn.cur.many[0][0]
    assert written[0] == "O:NVDA261120C00245000" and written[6] == date(2026, 5, 4)
    # ON CONFLICT keeps the earliest first_pinned and the longest pin, and no
    # statement deletes: a row past its pin_until simply stops being read.
    sql = conn.cur.many_sql[0]
    assert "LEAST(" in sql and "GREATEST(" in sql
    assert "DELETE" not in sql.upper()
    assert pinned.write_pins(_Conn(), []) == 0


def test_a_down_trade_api_skips_the_round_instead_of_emptying_the_table(monkeypatch) -> None:
    calls: list[str] = []

    def _boom(base: str, path: str, params: Any = None) -> Any:
        raise RuntimeError("trade api GET unreachable: connection refused")

    monkeypatch.setattr("bifrost_research.mcp.tools._trade_api_client.get", _boom)
    monkeypatch.setattr(
        "bifrost_research.engines.option_pinned.entry.connect",
        lambda: calls.append("connected") or _Conn(),
    )
    result = pinned.run(as_of=TODAY)
    assert result["mode"] == "skipped" and result["rows_written"] == 0
    assert "trade api unavailable" in result["reason"]
    # Nothing was written, and the database was not even opened.
    assert calls == []


def test_the_happy_path_reads_positions_and_executions_and_writes_pins(monkeypatch) -> None:
    def _get(base: str, path: str, params: Any = None) -> Any:
        if path == "/executions/position-attribution":
            assert params == {"sec_type": "OPT"}
            return {
                "attributions": [
                    {"contract_key": "NVDA|OPT|20261120|245.0|C", "sec_type": "OPT"},
                    # The same position seen through a second instance is one contract.
                    {"contract_key": "NVDA|OPT|20261120|245.0|C", "sec_type": "OPT"},
                    {"contract_key": "GOOG|OPT|20261218|300.0|P", "sec_type": "OPT"},
                ]
            }
        assert path == "/executions"
        since = datetime.fromtimestamp(params["since_ts"], tz=timezone.utc).date()
        assert since < TODAY - timedelta(days=pinned.CLOSED_RECENT_DAYS)
        return {
            "executions": [
                _exec_row("NVDA  261120C00245000|OPT|20261120|245.0|C", "2026-06-01"),
                _exec_row("MU    261016P00180000|OPT|20261016|180.0|P", "2026-08-20"),
                _exec_row("NVDA|STK|||", "2026-08-20", sec_type="STK"),
            ]
        }

    catalog_rows = [
        ("O:NVDA261120C00245000", "NVDA", date(2026, 11, 20), 245.0, "C"),
        ("O:MU261016P00180000", "MU", date(2026, 10, 16), 180.0, "P"),
    ]
    conn = _Conn(catalog_rows)
    monkeypatch.setattr("bifrost_research.mcp.tools._trade_api_client.get", _get)
    monkeypatch.setattr("bifrost_research.engines.option_pinned.entry.connect", lambda: conn)
    result = pinned.run(as_of=TODAY)
    assert result["mode"] == "written"
    assert result["held_legs"] == 2 and result["closed_recent_legs"] == 1
    assert result["rows_written"] == 2  # GOOG is not in the catalog
    assert result["unmatched_count"] == 1 and result["unmatched"][0]["underlying"] == "GOOG"
    assert result["executions_seen"] == 2  # the stock leg is not an option
    written = {r[0]: r for r in conn.cur.many[0]}
    assert set(written) == {"O:NVDA261120C00245000", "O:MU261016P00180000"}
    assert written["O:NVDA261120C00245000"][5] == "held"
    assert written["O:NVDA261120C00245000"][6] == date(2026, 6, 1)  # first_pinned
    assert written["O:MU261016P00180000"][5] == "closed_recent"
