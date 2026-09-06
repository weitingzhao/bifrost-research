"""Verdict, track record and similar summary — the enrichment every exhibit carries."""

from __future__ import annotations

from typing import Any

from bifrost_research.lenses.registry import classify_category, public_registry
from bifrost_research.lenses.similar import summarize_forward_returns
from bifrost_research.lenses.track_record import fetch_track_record
from bifrost_research.lenses.verdict import verdict_for


def test_verdict_bands_and_meanings() -> None:
    hot = verdict_for("iv_rank", 85)
    assert hot is not None and hot["band"] == "hot" and hot["label"] == "Hot"
    assert "short-premium" in hot["means"]
    assert verdict_for("iv_rank", 0.85, fractions_as_pct=True)["band"] == "hot"
    mid = verdict_for("vrp", 50)
    assert mid["band"] == "neutral" and "confirm" in mid["means"]
    calm = verdict_for("skew", 0.03)
    assert calm["band"] == "neutral" and calm["means"] == "Skew calm — structures are freer."
    assert verdict_for("iv_rank", None) is None
    assert verdict_for("term_slope", 0.4) is None


def test_categorical_verdicts() -> None:
    assert classify_category("terrain_regime", "Crash-Risk") == "hot"
    assert classify_category("gex_regime", "negative") == "hot"
    assert classify_category("gex_regime", "sideways") is None
    v = verdict_for("terrain_regime", "range")
    assert v is not None and v["band"] == "neutral" and v["value"] == "range"
    assert verdict_for("gex_regime", None) is None
    assert public_registry()[0]["categories"] == {}
    terrain = next(r for r in public_registry() if r["id"] == "terrain_regime")
    assert terrain["categories"]["crash-risk"] == "hot"


def test_similar_summary_uses_resolved_rows_only() -> None:
    rows = [
        {"fwd_return": 0.02},
        {"fwd_return": -0.01},
        {"fwd_return": None},
        {"fwd_return": 0.05},
        {"fwd_return": 0.00},
    ]
    s = summarize_forward_returns(rows, horizon=5)
    assert s["n"] == 5 and s["n_resolved"] == 4
    assert s["median_fwd"] == 0.01
    assert s["p25_fwd"] < s["median_fwd"] < s["p75_fwd"]
    assert s["share_positive"] == 0.5
    empty = summarize_forward_returns([{"fwd_return": None}], horizon=20)
    assert empty["n_resolved"] == 0 and empty["median_fwd"] is None


class _Cur:
    def __init__(self, by_symbol: list[tuple[Any, ...]], all_rows: list[tuple[Any, ...]]) -> None:
        self._by_symbol = by_symbol
        self._all = all_rows
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self._rows = self._by_symbol if "symbol = %s" in sql else self._all

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, by_symbol: list[tuple[Any, ...]], all_rows: list[tuple[Any, ...]]) -> None:
        self.by_symbol = by_symbol
        self.all_rows = all_rows

    def cursor(self) -> _Cur:
        return _Cur(self.by_symbol, self.all_rows)

    def rollback(self) -> None:  # pragma: no cover — only on failure
        pass


def test_track_record_scopes_to_symbol_when_it_has_enough_rows() -> None:
    scoped = [("hot", True, True)] * 12 + [("cold", False, None)] * 3
    all_rows = [("hot", False, False)] * 40
    tr = fetch_track_record(_Conn(scoped, all_rows), "iv_rank", "NVDA")
    assert tr is not None and tr["symbol_scoped"] is True
    assert tr["n"] == 15 and tr["hit_rate_5d"] == 0.8
    assert tr["by_side"]["hot"]["hit_rate_20d"] == 1.0
    assert tr["by_side"]["cold"]["evaluated_20d"] == 0 and tr["by_side"]["cold"]["hit_rate_20d"] is None


def test_track_record_falls_back_to_all_symbols_and_says_so() -> None:
    tr = fetch_track_record(_Conn([("hot", True, None)] * 2, [("hot", False, False)] * 4), "vrp", "XYZ")
    assert tr is not None and tr["symbol_scoped"] is False and tr["n"] == 4
    assert fetch_track_record(_Conn([], []), "vrp", "XYZ") is None
