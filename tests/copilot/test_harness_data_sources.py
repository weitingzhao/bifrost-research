"""Wave Y.1 — harness data_sources adapter (in-process reader for features.*)."""

from __future__ import annotations

from typing import Any

import pytest

from bifrost_research.copilot.harness import data_sources as ds


class _FakeCursor:
    def __init__(self, rows: list[tuple] | None, columns: list[str] | None = None) -> None:
        self._rows = list(rows or [])
        self._cols = list(columns or [])
        self.executed: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    @property
    def description(self) -> list[tuple[str]]:
        return [(c,) for c in self._cols]

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed.append((sql, tuple(params)))

    def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[tuple] | None, columns: list[str] | None = None) -> None:
        self._cur = _FakeCursor(rows, columns)

    def cursor(self) -> _FakeCursor:
        return self._cur


class _RaisingConn:
    def cursor(self) -> Any:
        raise RuntimeError("db unreachable")


# ------------------ parse_flag_filter ------------------


def test_parse_flag_filter_empty() -> None:
    assert ds.parse_flag_filter(None) == []
    assert ds.parse_flag_filter("") == []
    assert ds.parse_flag_filter("   ") == []


def test_parse_flag_filter_valid() -> None:
    assert ds.parse_flag_filter("iv_rank:hot,vrp:cold") == [
        ("iv_rank", "hot"),
        ("vrp", "cold"),
    ]


def test_parse_flag_filter_bad_segment() -> None:
    with pytest.raises(ValueError):
        ds.parse_flag_filter("iv_rank")


def test_parse_flag_filter_unknown_key() -> None:
    with pytest.raises(ValueError):
        ds.parse_flag_filter("garbage:hot")


def test_parse_flag_filter_unknown_value() -> None:
    with pytest.raises(ValueError):
        ds.parse_flag_filter("iv_rank:onfire")


# ------------------ top_scan_symbols ------------------


def _scan_row(
    symbol: str,
    score: float,
    *,
    iv_rank: float = 0.9,
    vrp: float = 0.5,
    atm: float = 0.1,
    pin_pct: float = 0.0,
    pin_score: float = 50.0,
    flags: dict | None = None,
) -> tuple:
    return (
        "2026-08-28",
        symbol,
        100.0,
        iv_rank,
        vrp,
        atm,
        pin_pct,
        score,
        flags or {"iv_rank": "hot"},
        "bull",
        pin_score,
    )


def test_top_scan_symbols_returns_rows() -> None:
    conn = _FakeConn(
        rows=[_scan_row("AAPL", 82.0), _scan_row("MSFT", 78.0)],
        columns=list(ds._SCAN_COLUMNS),
    )
    rows = ds.top_scan_symbols(conn, limit=2)
    assert len(rows) == 2
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["composite_score"] == 82.0


def test_top_scan_symbols_applies_flag_filter() -> None:
    conn = _FakeConn(rows=[_scan_row("AAPL", 82.0)], columns=list(ds._SCAN_COLUMNS))
    ds.top_scan_symbols(conn, limit=3, flag_filter="iv_rank:hot,vrp:hot")
    sql, params = conn._cur.executed[-1]
    # Both flag keys appear in params, values follow
    assert "iv_rank" in params and "vrp" in params
    assert params.count("hot") == 2
    assert "lens_flags ->> %s = %s" in sql


def test_top_scan_symbols_applies_min_composite() -> None:
    conn = _FakeConn(rows=[], columns=list(ds._SCAN_COLUMNS))
    ds.top_scan_symbols(conn, limit=5, min_composite_score=60.0)
    sql, params = conn._cur.executed[-1]
    assert "composite_score IS NOT NULL AND composite_score >= %s" in sql
    assert 60.0 in params


def test_top_scan_symbols_fails_soft_on_error() -> None:
    rows = ds.top_scan_symbols(_RaisingConn(), limit=5)
    assert rows == []


def test_top_scan_symbols_ignores_bad_flag_filter() -> None:
    conn = _FakeConn(rows=[], columns=list(ds._SCAN_COLUMNS))
    # Malformed flag filter is logged + ignored (not raised)
    rows = ds.top_scan_symbols(conn, limit=3, flag_filter="garbage")
    assert rows == []
    sql, _params = conn._cur.executed[-1]
    # No flag-key filter injected (SELECT column still names lens_flags)
    assert "lens_flags ->>" not in sql


def test_top_scan_symbols_limit_clamp() -> None:
    conn = _FakeConn(rows=[], columns=list(ds._SCAN_COLUMNS))
    ds.top_scan_symbols(conn, limit=999)
    sql, _ = conn._cur.executed[-1]
    assert "LIMIT 50" in sql


def test_top_scan_symbols_preset_momentum_differs_from_neutral() -> None:
    from bifrost_research.api.scan import COMPOSITE_PRESETS, recompute_composite

    high_iv = _scan_row(
        "AAA",
        90.0,
        iv_rank=95.0,
        vrp=90.0,
        atm=0.0,
        pin_pct=0.0,
        pin_score=20.0,
    )
    high_mom = _scan_row(
        "BBB",
        90.0,
        iv_rank=20.0,
        vrp=20.0,
        atm=0.4,
        pin_pct=0.0,
        pin_score=90.0,
    )
    cols = list(ds._SCAN_COLUMNS)
    row_a = dict(zip(cols, high_iv))
    row_b = dict(zip(cols, high_mom))
    neu_a = recompute_composite(row_a, COMPOSITE_PRESETS["neutral"])
    neu_b = recompute_composite(row_b, COMPOSITE_PRESETS["neutral"])
    mom_a = recompute_composite(row_a, COMPOSITE_PRESETS["momentum"])
    mom_b = recompute_composite(row_b, COMPOSITE_PRESETS["momentum"])
    assert mom_a is not None and mom_b is not None
    assert neu_a is not None and neu_b is not None
    assert (mom_a > mom_b) != (neu_a > neu_b) or abs(mom_a - neu_a) > 1.0

    neu_conn = _FakeConn(rows=[high_iv, high_mom], columns=cols)
    neu_rows = ds.top_scan_symbols(neu_conn, limit=2, preset="neutral")
    mom_conn = _FakeConn(rows=[high_iv, high_mom], columns=cols)
    mom_rows = ds.top_scan_symbols(mom_conn, limit=2, preset="momentum")
    assert [r["symbol"] for r in neu_rows] == ["AAA", "BBB"]
    assert [r["symbol"] for r in mom_rows] == ["BBB", "AAA"]
    assert mom_rows[0]["composite_score"] != neu_rows[0]["composite_score"]
    sql, _ = mom_conn._cur.executed[-1]
    assert "LIMIT 500" in sql


def test_top_scan_symbols_unknown_preset_uses_stored() -> None:
    conn = _FakeConn(rows=[_scan_row("AAA", 80.0)], columns=list(ds._SCAN_COLUMNS))
    rows = ds.top_scan_symbols(conn, limit=1, preset="bogus")
    assert rows[0]["composite_source"] == "stored"
    sql, _ = conn._cur.executed[-1]
    assert "LIMIT 1" in sql


# ------------------ global_signal_decay_summary ------------------


def test_signal_decay_summary_all_lenses_empty() -> None:
    conn = _FakeConn(rows=[])
    out = ds.global_signal_decay_summary(conn)
    assert set(out.keys()) == {"iv_rank", "vrp", "opex_pin"}
    for lens in ("iv_rank", "vrp", "opex_pin"):
        assert out[lens]["n"] == 0
        assert out[lens]["hit_rate_5d"] is None


def test_signal_decay_summary_computes_rates() -> None:
    # (lens, n, eval5, ok5, eval20, ok20)
    conn = _FakeConn(
        rows=[
            ("iv_rank", 20, 15, 9, 10, 5),
            ("vrp", 10, 10, 4, 8, 2),
        ]
    )
    out = ds.global_signal_decay_summary(conn)
    assert out["iv_rank"]["n"] == 20
    assert out["iv_rank"]["hit_rate_5d"] == round(9 / 15, 4)
    assert out["iv_rank"]["hit_rate_20d"] == round(5 / 10, 4)
    assert out["vrp"]["hit_rate_5d"] == round(4 / 10, 4)
    assert out["opex_pin"]["n"] == 0


def test_signal_decay_summary_fails_soft() -> None:
    out = ds.global_signal_decay_summary(_RaisingConn())
    for lens in ("iv_rank", "vrp", "opex_pin"):
        assert out[lens]["n"] == 0


def test_signal_decay_summary_window_clamp() -> None:
    conn = _FakeConn(rows=[])
    ds.global_signal_decay_summary(conn, window_days=99999)
    _, params = conn._cur.executed[-1]
    # window_days clamped to 400; SQL uses window_days + 5 → 405
    assert 405 in params
