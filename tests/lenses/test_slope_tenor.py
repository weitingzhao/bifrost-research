"""Guard: every skew reader takes the ~30-day slope from ``lenses/slope_tenor.py``.

Five readers once picked the fit nearest 30 DTE at any tenor while one kept to
20–45, so a page could rank a name on a slope another page did not have. These
tests pin the single definition (A′, 2026-09-30: interpolated to 30 DTE between
the fits either side, else the fit nearest 30 within 20–45): no reader may pick
a fit on its own, and each named reader binds the definition's ``where`` as
many times as it repeats it. All values here are invented.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from bifrost_research.lenses import exhibit_lenses
from bifrost_research.lenses.slope_tenor import (
    SLOPE_30D_COLUMNS,
    SLOPE_30D_WHERE_BINDS,
    slope_30d_sql,
)

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
    assert offenders == [], f"pick the ~30-day reading via lenses/slope_tenor.py: {offenders}"


def test_each_reader_uses_the_shared_reading() -> None:
    for rel in READERS:
        text = (_SRC_ROOT / rel).read_text(encoding="utf-8")
        assert "slope_30d_sql(" in text, rel
        assert "SLOPE_30D_WHERE_BINDS" in text, rel


def test_the_where_is_repeated_as_many_times_as_declared() -> None:
    sql = slope_30d_sql("symbol = %s AND trade_date = %s")
    assert sql.count("%s") == 2 * SLOPE_30D_WHERE_BINDS
    assert slope_30d_sql().count("%s") == 0


def test_the_reading_interpolates_first_and_falls_back_to_the_window() -> None:
    sql = " ".join(slope_30d_sql().split())
    # Both sides of 30 DTE, within the fit table's own 7–90.
    assert "dte BETWEEN 7 AND 30" in sql and "dte BETWEEN 30 AND 90" in sql
    # The 0.140.0 window is the fallback, ranked after the interpolation.
    assert "dte BETWEEN 20 AND 45" in sql and "ABS(dte - 30) ASC, expiry ASC" in sql
    assert sql.index("'interpolated'") < sql.index("'window'")
    assert "ORDER BY symbol, trade_date, basis_rank" in sql
    # Every declared column is selected at the top.
    head = sql.split(" FROM ", 1)[0]
    for col in SLOPE_30D_COLUMNS:
        assert col in head, col


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


def test_exhibit_reading_binds_with_and_without_a_cut() -> None:
    conn = _Conn()
    exhibit_lenses.skew_fit_row(conn, "NVDA")
    exhibit_lenses.skew_fit_row(conn, "NVDA", "2026-08-20")
    (now_sql, now_params), (cut_sql, cut_params) = conn.calls
    assert now_params == ("NVDA",) * (1 + SLOPE_30D_WHERE_BINDS)
    assert cut_params == ("NVDA", "2026-08-20") * (1 + SLOPE_30D_WHERE_BINDS)
    for sql, params in conn.calls:
        _binds(sql, params)
        assert "'interpolated'" in sql
    assert "trade_date < %s::date" in cut_sql and "trade_date < %s::date" not in now_sql


def test_exhibit_history_reads_the_same_reading() -> None:
    conn = _Conn()
    exhibit_lenses.skew_slope_pctile(conn, "NVDA", date(2026, 8, 25), 0.03)
    sql, params = conn.calls[0]
    _binds(sql, params)
    assert "'interpolated'" in sql


def _row(**over: Any) -> tuple[Any, ...]:
    base = {
        "trade_date": date(2026, 8, 21),
        "expiry": date(2026, 9, 18),
        "dte": 30,
        "atm_vol": 0.31,
        "atm_slope": 0.04,
        "fit_rmse": 0.05,
        "n_points": 14,
        "computed_at": None,
        "latest_fit": date(2026, 8, 21),
        "basis": "interpolated",
        "short_expiry": date(2026, 9, 11),
        "short_dte": 21,
        "long_expiry": date(2026, 10, 16),
        "long_dte": 56,
    }
    base.update(over)
    return tuple(base.values())


def test_exhibit_says_why_the_reading_is_older(monkeypatch) -> None:
    monkeypatch.setattr(exhibit_lenses, "skew_fit_row", lambda _c, _s: _row(latest_fit=date(2026, 8, 25)))
    monkeypatch.setattr(exhibit_lenses, "skew_slope_pctile", lambda *_a: (80, 0.02, 71.0))
    exh = exhibit_lenses.exhibit_skew(_Conn(), "NVDA")
    assert exh.as_of == "2026-08-21"
    assert any(
        "2026-08-25" in c and "either side of 30 DTE" in c and "2026-08-21" in c for c in exh.caveats
    )

    monkeypatch.setattr(exhibit_lenses, "skew_fit_row", lambda _c, _s: _row())
    exh = exhibit_lenses.exhibit_skew(_Conn(), "NVDA")
    assert not any("either side of 30 DTE" in c for c in exh.caveats)
    assert exh.readings["basis"] == "interpolated"
    assert (exh.readings["short_dte"], exh.readings["long_dte"]) == (21, 56)
    assert exh.readings["short_expiry"] == "2026-09-11"


def test_exhibit_says_when_the_slope_is_one_fits(monkeypatch) -> None:
    """A window reading is one expiry's slope, and the page must be able to say so."""
    row = _row(dte=32, basis="window", short_expiry=None, short_dte=None, long_expiry=None, long_dte=None)
    monkeypatch.setattr(exhibit_lenses, "skew_fit_row", lambda _c, _s: row)
    monkeypatch.setattr(exhibit_lenses, "skew_slope_pctile", lambda *_a: (80, 0.02, 71.0))
    exh = exhibit_lenses.exhibit_skew(_Conn(), "NVDA")
    assert exh.readings["basis"] == "window" and exh.readings["short_dte"] is None
    assert any("32-DTE fit's, not interpolated to 30" in c for c in exh.caveats)


def test_exhibit_without_a_reading_says_why(monkeypatch) -> None:
    monkeypatch.setattr(exhibit_lenses, "skew_fit_row", lambda _c, _s: None)
    monkeypatch.setattr(exhibit_lenses, "skew_slope_pctile", lambda *_a: None)
    exh = exhibit_lenses.exhibit_skew(_Conn(), "NVDA")
    assert any("either side of 30 DTE" in c and "20–45 DTE" in c for c in exh.caveats)


def test_signal_hit_skew_trigger_binds_the_reading() -> None:
    from bifrost_research.engines.signal_hit import entry

    conn = _Conn()
    assert entry._load_skew_triggers(conn, date(2026, 8, 25)) == []
    sql, params = conn.calls[0]
    _binds(sql, params)
    assert sql.count("'interpolated'") == 2


def test_scan_reads_the_reading_and_binds_it() -> None:
    from bifrost_research.engines.scan import entry

    cte = entry._AGGREGATE_SQL.split("surface_30d AS (", 1)[1].split("\n),", 1)[0]
    assert "'interpolated'" in cte
    assert cte.count("%s") == SLOPE_30D_WHERE_BINDS

    conn = _Conn()

    class _DescCur(_Cur):
        description: list[tuple[str]] = []

    conn.cursor = lambda: _DescCur(conn)  # type: ignore[method-assign]
    entry.fetch_scan_source_rows(conn, date(2026, 8, 25), ["NVDA"])
    sql, params = conn.calls[0]
    _binds(sql, params)


def test_similar_regime_reads_the_reading() -> None:
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
    assert "'interpolated'" in sql
    assert params[:SLOPE_30D_WHERE_BINDS] == ("NVDA",) * SLOPE_30D_WHERE_BINDS


def test_skew_extremes_reads_bind() -> None:
    from bifrost_research.repositories import vol_surface as repo

    conn = _Conn(one=(0,))
    repo.get_skew_extremes(conn, as_of=date(2026, 9, 29), limit=5)
    repo.count_skew_names(conn, as_of=date(2026, 9, 29))
    repo.get_skew_left_out(conn, as_of=date(2026, 9, 29))
    for sql, params in conn.calls:
        _binds(sql, params)
        assert "'interpolated'" in sql
