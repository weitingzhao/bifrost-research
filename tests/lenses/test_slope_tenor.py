"""Guard: every skew reader takes the ~30-day slope from ``lenses/slope_tenor.py``.

Five readers once picked the fit nearest 30 DTE at any tenor while one kept to
20–45, so a page could rank a name on a slope another page did not have. These
tests pin the single definition: no reader may pick "nearest 30" on its own,
and each named reader binds the window and the right number of parameters.
All values here are invented.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from bifrost_research.lenses import exhibit_lenses
from bifrost_research.lenses.slope_tenor import SLOPE_PICK_ORDER, slope_window_sql

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "bifrost_research"
_NEAREST_30 = re.compile(r"ABS\(\s*dte\s*-\s*30\s*\)")

READERS = (
    "repositories/vol_surface.py",
    "engines/scan/entry.py",
    "engines/signal_hit/entry.py",
    "lenses/exhibit_lenses.py",
    "api/similar_regime.py",
)


def test_no_reader_picks_nearest_30_on_its_own() -> None:
    offenders = [
        str(p.relative_to(_SRC_ROOT))
        for p in sorted(_SRC_ROOT.rglob("*.py"))
        if p.name != "slope_tenor.py" and _NEAREST_30.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"pick the ~30-day fit via lenses/slope_tenor.py: {offenders}"


def test_each_reader_uses_the_shared_window() -> None:
    for rel in READERS:
        text = (_SRC_ROOT / rel).read_text(encoding="utf-8")
        assert "slope_window_sql(" in text, rel
        assert "SLOPE_PICK_ORDER" in text, rel


def test_window_predicate() -> None:
    assert slope_window_sql() == "atm_slope IS NOT NULL AND dte BETWEEN 20 AND 45"
    assert slope_window_sql("f") == "f.atm_slope IS NOT NULL AND f.dte BETWEEN 20 AND 45"
    assert SLOPE_PICK_ORDER.startswith("ABS(dte - 30)")


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._conn.calls.append((sql, tuple(params)))

    def fetchone(self) -> Any:
        return self._conn.one

    def fetchall(self) -> list[Any]:
        return []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _Conn:
    def __init__(self, one: Any = None) -> None:
        self.one = one
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def rollback(self) -> None:
        return None


def _binds(sql: str, params: tuple[Any, ...]) -> None:
    assert sql.count("%s") == len(params), (sql.count("%s"), params)


def test_exhibit_reading_binds_the_window_with_and_without_a_cut() -> None:
    conn = _Conn()
    exhibit_lenses.skew_fit_row(conn, "NVDA")
    exhibit_lenses.skew_fit_row(conn, "NVDA", "2026-08-20")
    (now_sql, now_params), (cut_sql, cut_params) = conn.calls
    assert now_params == ("NVDA", "NVDA", "NVDA")
    assert cut_params == ("NVDA", "2026-08-20", "NVDA", "NVDA", "2026-08-20")
    for sql, params in conn.calls:
        _binds(sql, params)
        assert sql.count(slope_window_sql()) == 2
    assert "trade_date < %s::date" in cut_sql and "trade_date < %s::date" not in now_sql


def test_exhibit_history_reads_the_same_window() -> None:
    conn = _Conn()
    exhibit_lenses.skew_slope_pctile(conn, "NVDA", date(2026, 8, 25), 0.03)
    sql, params = conn.calls[0]
    _binds(sql, params)
    assert slope_window_sql() in sql


def test_exhibit_says_why_the_reading_is_older(monkeypatch) -> None:
    row = (date(2026, 8, 21), date(2026, 9, 18), 28, 0.31, 0.04, 0.05, 14, None, date(2026, 8, 25))
    monkeypatch.setattr(exhibit_lenses, "skew_fit_row", lambda _c, _s: row)
    monkeypatch.setattr(exhibit_lenses, "skew_slope_pctile", lambda *_a: (80, 0.02, 71.0))
    exh = exhibit_lenses.exhibit_skew(_Conn(), "NVDA")
    assert exh.as_of == "2026-08-21"
    assert any("2026-08-25" in c and "20–45 DTE" in c and "2026-08-21" in c for c in exh.caveats)

    same_day = row[:8] + (date(2026, 8, 21),)
    monkeypatch.setattr(exhibit_lenses, "skew_fit_row", lambda _c, _s: same_day)
    exh = exhibit_lenses.exhibit_skew(_Conn(), "NVDA")
    assert not any("20–45 DTE" in c for c in exh.caveats)


def test_signal_hit_skew_trigger_binds_the_window() -> None:
    from bifrost_research.engines.signal_hit import entry

    conn = _Conn()
    assert entry._load_skew_triggers(conn, date(2026, 8, 25)) == []
    sql, params = conn.calls[0]
    _binds(sql, params)
    assert sql.count(slope_window_sql()) == 2


def test_scan_reads_the_window() -> None:
    from bifrost_research.engines.scan import entry

    cte = entry._AGGREGATE_SQL.split("surface_30d AS (", 1)[1].split("),", 1)[0]
    assert slope_window_sql() in cte
    assert SLOPE_PICK_ORDER in cte


def test_similar_regime_reads_the_window() -> None:
    from bifrost_research.api import similar_regime

    class _DescCur(_Cur):
        description: list[tuple[str]] = []

    class _DescConn(_Conn):
        def cursor(self) -> _DescCur:
            return _DescCur(self)

    conn = _DescConn()
    rows, _source = similar_regime._similar_term_slope(conn, symbol="nvda", value=0.03, k=5, horizon=20)
    assert rows == []
    sql, params = conn.calls[0]
    _binds(sql, params)
    assert slope_window_sql() in sql
