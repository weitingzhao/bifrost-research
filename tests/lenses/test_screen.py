"""The bulk screening primitive — C-F1, and the seam contract's precondition.

What matters here is not the SQL text but three properties: one statement per
lens rather than one per symbol, bands that come from the registry rather than
from this module, and a row that names the faces a symbol does not have.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from bifrost_research.lenses import screen as mod
from bifrost_research.lenses.registry import LENSES, classify


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self.conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        if sql.startswith("SET LOCAL"):
            self.conn.timeouts += 1
            return
        self.conn.queries.append((sql, params))
        if "research.option_universe" in sql:
            self._rows = [(s,) for s in self.conn.universe]
            return
        for marker, rows in self.conn.answers.items():
            if marker in sql:
                self._rows = rows
                return
        self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    def __init__(self, universe: list[str], answers: dict[str, list[tuple[Any, ...]]]) -> None:
        self.universe = universe
        self.answers = answers
        self.queries: list[tuple[str, Any]] = []
        self.timeouts = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


AS_OF = date(2026, 9, 8)


def _conn() -> _FakeConn:
    return _FakeConn(
        universe=["AAPL", "MSFT", "NVDA"],
        answers={
            # iv_rank: NVDA cold, AAPL hot, MSFT absent
            "option_metric_iv_percentile_daily": [
                ("AAPL", 88.0, date(2026, 9, 4)),
                ("NVDA", 12.0, date(2026, 9, 4)),
            ],
            # sepa: all three
            "stock_signal_sepa_daily": [
                ("AAPL", 84.0, date(2026, 9, 5)),
                ("MSFT", 55.0, date(2026, 9, 5)),
                ("NVDA", 81.0, date(2026, 9, 5)),
            ],
            "option_metric_gex_levels_daily": [("AAPL", "negative", date(2026, 9, 4))],
        },
    )


def test_one_statement_per_lens_not_one_per_symbol() -> None:
    conn = _conn()

    mod.screen(conn, lenses=["iv_rank", "sepa"], as_of=AS_OF)

    # Three symbols, two lenses: two data statements, plus the universe read.
    assert len(conn.queries) == 3
    assert sum(1 for sql, _ in conn.queries if "option_metric_iv_percentile_daily" in sql) == 1
    assert sum(1 for sql, _ in conn.queries if "stock_signal_sepa_daily" in sql) == 1
    # Every statement carries the universe as one array parameter.
    for sql, params in conn.queries:
        if "option_universe" in sql:
            continue
        assert params["symbols"] == ["AAPL", "MSFT", "NVDA"]
        assert params["since"] == date(2026, 9, 1)  # as_of minus the default window
    assert conn.timeouts == len(conn.queries)  # each one bounded


def test_bands_come_from_the_registry() -> None:
    conn = _conn()

    result = mod.screen(conn, lenses=["iv_rank", "sepa"], as_of=AS_OF)

    rows = {r.symbol: r for r in result.rows}
    assert rows["AAPL"].readings["iv_rank"].band == classify("iv_rank", 88.0) == "hot"
    assert rows["NVDA"].readings["iv_rank"].band == classify("iv_rank", 12.0) == "cold"
    assert rows["MSFT"].readings["sepa"].band == classify("sepa", 55.0)
    assert rows["AAPL"].readings["iv_rank"].as_of == date(2026, 9, 4)


def test_a_row_names_the_faces_it_does_not_have() -> None:
    conn = _conn()

    result = mod.screen(conn, lenses=["iv_rank", "sepa", "gex_regime"], as_of=AS_OF)

    rows = {r.symbol: r for r in result.rows}
    assert rows["MSFT"].missing == ("iv_rank", "gex_regime")  # sepa only
    assert rows["AAPL"].missing == ()
    assert rows["NVDA"].missing == ("gex_regime",)
    assert result.coverage == {"iv_rank": 2, "sepa": 3, "gex_regime": 1}


def test_a_missing_face_does_not_survive_a_requirement_on_it() -> None:
    conn = _conn()

    result = mod.screen(
        conn,
        lenses=["iv_rank", "sepa"],
        require={"sepa": ["hot"], "iv_rank": ["cold", "lean_cold"]},
        as_of=AS_OF,
    )

    # NVDA: sepa 81 hot and iv_rank 12 cold. AAPL: sepa hot but iv_rank hot.
    # MSFT: sepa mid and no iv_rank at all — a face it cannot see never passes.
    assert [r.symbol for r in result.survivors] == ["NVDA"]


def test_a_categorical_lens_is_banded_by_its_category_not_its_number() -> None:
    conn = _conn()

    result = mod.screen(conn, lenses=["gex_regime"], as_of=AS_OF)

    aapl = next(r for r in result.rows if r.symbol == "AAPL")
    assert aapl.readings["gex_regime"].value == "negative"
    assert aapl.readings["gex_regime"].band == "hot"  # negative gamma — dealers chase
    assert "CASE WHEN total_net_gex < 0" in conn.queries[1][0]


def test_a_lens_that_cannot_be_screened_says_so_instead_of_answering() -> None:
    conn = _conn()

    result = mod.screen(conn, lenses=["sepa", "skew", "term_slope"], as_of=AS_OF)

    assert set(result.unscreenable) == {"skew", "term_slope"}
    assert "252-day percentile" in result.unscreenable["skew"]
    # It was asked for, so it stays in `lenses`; it is not counted as missing
    # per symbol, because no symbol could have had it.
    assert result.lenses == ("sepa", "skew", "term_slope")
    assert all(r.missing == () for r in result.rows)
    assert "skew" not in result.coverage
    assert mod.lens_sql("skew") is None


def test_the_universe_can_be_a_tier_or_an_explicit_list() -> None:
    conn = _conn()
    mod.screen(conn, lenses=["sepa"], tiers=["core", "edge"], as_of=AS_OF)
    universe_sql, params = conn.queries[0]
    assert "tier = ANY(%s)" in universe_sql and params == [["core", "edge"]]

    conn2 = _conn()
    result = mod.screen(conn2, lenses=["sepa"], symbols=["MSFT"], as_of=AS_OF)
    assert result.universe == ("MSFT",)
    assert all("option_universe" not in sql for sql, _ in conn2.queries)


def test_every_registry_lens_is_either_screenable_or_explained() -> None:
    for lens_id in LENSES:
        assert (mod.lens_sql(lens_id) is not None) ^ (lens_id in mod.UNSCREENABLE), lens_id


def test_an_unknown_lens_is_refused_before_any_query_runs() -> None:
    conn = _conn()
    with pytest.raises(ValueError, match="unknown lens: nope"):
        mod.screen(conn, lenses=["sepa", "nope"], as_of=AS_OF)
    assert conn.queries == []
