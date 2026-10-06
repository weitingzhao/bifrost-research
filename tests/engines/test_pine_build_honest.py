"""Pine build, 0.175.0: warm-up, renames, delisted names (engines/pine/build.py)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from bifrost_research.engines.pine import build
from bifrost_research.engines.pine.library import PineScript
from bifrost_research.repositories import listing_lineage

D0 = date(2024, 1, 1)


def _bars(n: int) -> list[dict[str, Any]]:
    return [{"date": D0 + timedelta(days=i), "close": 10.0 + i} for i in range(n)]


def test_signals_inside_the_warm_up_are_not_written() -> None:
    s = PineScript(id="x", name="x", source='plotshape(true, "buy")', version=1)
    bars = {"AAA": _bars(150), "NEW": _bars(60)}
    fired = {
        "AAA": {"buy": [D0, D0 + timedelta(days=99), D0 + timedelta(days=100), D0 + timedelta(days=140)], "sell": []},
        # A name listed for fewer bars than the warm-up writes nothing yet.
        "NEW": {"buy": [D0 + timedelta(days=59)], "sell": []},
    }
    rows, errors = build.signal_rows(s, bars, fired, None, warmup_bars=100)
    assert [r[2] for r in rows] == [D0 + timedelta(days=100), D0 + timedelta(days=140)]
    assert errors == {}
    # Without a warm-up every signal is kept (the pre-0.175.0 behaviour).
    rows, _ = build.signal_rows(s, bars, fired, None)
    assert len(rows) == 5


class _Conn:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows, self.sql, self.params = rows, "", None

    def cursor(self) -> "_Conn":
        return self

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def rollback(self) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql, self.params = " ".join(sql.split()), params

    def fetchall(self) -> list[tuple]:
        return self.rows


def test_bars_are_spliced_across_a_rename_and_keyed_by_the_live_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(listing_lineage, "_PREDECESSOR", {"ECHO": "SATS"})
    monkeypatch.setattr(listing_lineage, "_SUCCESSOR", {"SATS": "ECHO"})
    monkeypatch.setattr(listing_lineage, "_HANDOVERS", {("SATS", "ECHO"): date(2026, 3, 16)})
    conn = _Conn([("ECHO", date(2026, 3, 13), 1.0, 1.0, 1.0, 1.0, 10), ("ECHO", date(2026, 3, 16), 2.0, 2.0, 2.0, 2.0, 10)])
    out = build.load_bars_many(conn, ["ECHO", "NVDA"], date(2026, 1, 1), date(2026, 4, 1))
    assert conn.params["tickers"] == ["ECHO", "NVDA", "SATS"]
    assert "CASE WHEN symbol = %(lin_dead_0)s THEN %(lin_live_0)s" in conn.sql
    assert conn.params["lin_cut_0"] == date(2026, 3, 16)
    assert [b["date"] for b in out["ECHO"]] == [date(2026, 3, 13), date(2026, 3, 16)]


def test_retired_names_are_inactive_with_options_and_outside_the_universe() -> None:
    conn = _Conn([("wbs",), ("AVB",)])
    assert build.retired_names(conn, ["SPY", "NVDA"], date(2024, 10, 1)) == ["WBS", "AVB"]
    assert "t.active IS FALSE" in conn.sql and "raw_market.option_daily" in conn.sql
    assert conn.params == {"universe": ["SPY", "NVDA"], "start": date(2024, 10, 1)}


def test_a_full_rebuild_runs_the_retired_names_and_an_incremental_one_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    script = PineScript(id="st", name="st", source="x", version=2)
    sent: list[list[str]] = []
    monkeypatch.setattr(build, "connect", lambda: _Conn([]))
    monkeypatch.setattr(build, "ensure_builtins", lambda conn: 0)
    monkeypatch.setattr(build, "list_scripts", lambda conn, active_only: [script])
    monkeypatch.setattr(build, "load_symbols_from_env_or_query", lambda conn, symbols=None: ["SPY"])
    monkeypatch.setattr(build, "retired_names", lambda conn, universe, start: ["WBS"])
    monkeypatch.setattr(build, "load_bars_many", lambda conn, chunk, s, e: (sent.append(list(chunk)), {})[1])
    monkeypatch.setattr(build, "built_versions", lambda conn: {"st": 1})
    out = build.run(as_of=date(2026, 10, 5))
    assert sent == [["SPY", "WBS"]] and out["retired_names"] == ["WBS"]
    sent.clear()
    monkeypatch.setattr(build, "built_versions", lambda conn: {"st": 2})
    out = build.run(as_of=date(2026, 10, 5))
    assert sent == [["SPY"]] and "retired_names" not in out
