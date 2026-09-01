"""Universe reach — how much of the warehouse the Loop can actually see.

The load-bearing property is that an unreadable layer reports NOT MEASURED, never
zero: "we could not count this" and "this layer is empty" lead to opposite
decisions, and a zero would make the reach ratio silently wrong too.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.api import universe_reach as ur


class _Cursor:
    def __init__(self, counts: dict[str, int | None]) -> None:
        self._counts = counts
        self._row: tuple | None = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        for table, value in self._counts.items():
            if table in sql:
                if value is None:
                    raise RuntimeError(f"statement timeout on {table}")
                self._row = (value,)
                return
        self._row = None

    def fetchone(self) -> tuple | None:
        return self._row


class _Conn:
    def __init__(self, counts: dict[str, int | None]) -> None:
        self._counts = counts

    def cursor(self) -> _Cursor:
        return _Cursor(self._counts)

    def close(self) -> None:
        pass


_FULL = {
    "raw_market.stock_daily": 14836,
    "raw_market.stock_financials": 4469,
    "features.stock_signal_sepa_daily": 3472,
    "features.stock_signal_scan_daily": 28,
    "raw_market.option_daily": 19,
}


def test_reach_reports_every_layer() -> None:
    d = ur.build_reach(_Conn(_FULL))
    counts = {layer["key"]: layer["symbols"] for layer in d["layers"]}
    assert counts["stock_daily"] == 14836
    assert counts["scan"] == 28
    assert d["measured"] is True


def test_reach_ratio_is_loop_over_widest() -> None:
    d = ur.build_reach(_Conn(_FULL))
    assert d["widest_symbols"] == 14836
    assert d["loop_symbols"] == 28
    assert d["loop_pct_of_widest"] == 0.19


def test_layers_are_ordered_widest_first() -> None:
    d = ur.build_reach(_Conn(_FULL))
    counts = [layer["symbols"] for layer in d["layers"][:4]]
    assert counts == sorted(counts, reverse=True)


def test_unreadable_layer_is_not_measured_not_zero() -> None:
    broken = {**_FULL, "features.stock_signal_sepa_daily": None}
    d = ur.build_reach(_Conn(broken))
    sepa = next(layer for layer in d["layers"] if layer["key"] == "sepa")
    assert sepa["symbols"] is None
    assert sepa["status"] == "unavailable"
    assert d["measured"] is False
    # Zero would read as "SEPA covers nothing" — a different claim entirely.
    assert sepa["symbols"] != 0


def test_ratio_withheld_when_an_end_of_the_funnel_is_unreadable() -> None:
    d = ur.build_reach(_Conn({**_FULL, "raw_market.stock_daily": None}))
    assert d["widest_symbols"] is None
    assert d["loop_pct_of_widest"] is None


def test_a_genuinely_empty_layer_still_reports_zero() -> None:
    """Zero is a legitimate answer when the query succeeds — only failure is null."""
    d = ur.build_reach(_Conn({**_FULL, "raw_market.option_daily": 0}))
    opt = next(layer for layer in d["layers"] if layer["key"] == "option_daily")
    assert opt["symbols"] == 0
    assert opt["status"] == "ok"


def test_endpoint_caches_then_refreshes(monkeypatch: Any) -> None:
    ur._cache = None
    ur._cache_at = 0.0
    calls: list[int] = []

    def _connect() -> _Conn:
        calls.append(1)
        return _Conn(_FULL)

    monkeypatch.setattr(ur, "connect", _connect)

    first = ur.get_universe_reach()
    assert first["data"]["cached"] is False
    second = ur.get_universe_reach()
    assert second["data"]["cached"] is True
    assert len(calls) == 1

    third = ur.get_universe_reach(refresh=True)
    assert third["data"]["cached"] is False
    assert len(calls) == 2


def test_reach_follows_the_active_universe_mode(monkeypatch: Any) -> None:
    """A stock_composite objective moves the Loop's reach off the scan table."""
    monkeypatch.setattr(ur, "_active_modes", lambda conn: ["stock_composite"])
    d = ur.build_reach(_Conn(_FULL))
    assert d["loop_symbols"] == 3472
    assert d["loop_pct_of_widest"] == 23.4
    assert d["universe_modes"] == ["stock_composite"]


def test_reach_uses_the_widest_mode_when_several_are_active(monkeypatch: Any) -> None:
    monkeypatch.setattr(ur, "_active_modes", lambda conn: ["scan_legacy", "stock_composite"])
    d = ur.build_reach(_Conn(_FULL))
    assert d["loop_symbols"] == 3472


def test_reach_falls_back_to_scan_when_modes_unknown(monkeypatch: Any) -> None:
    """An unreadable objective table must not silently widen the claim."""
    monkeypatch.setattr(ur, "_active_modes", lambda conn: [])
    d = ur.build_reach(_Conn(_FULL))
    assert d["loop_symbols"] == 28
