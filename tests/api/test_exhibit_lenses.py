"""Exhibit for every lens — readers, registry enrichment, aliases and the HTTP surface (no DB)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.lenses.exhibits import LENS_ALIASES, build_exhibit, exhibit_lens_names
from bifrost_research.lenses.registry import LENSES

# (predicate on the SQL, rows, column names) — column names feed cursor.description
# for readers that build dicts from it (the similar-regime k-NN does).
Route = tuple[Callable[[str], bool], list[tuple[Any, ...]]] | tuple[Callable[[str], bool], list[tuple[Any, ...]], list[str]]
NOW = datetime.now(timezone.utc)
TODAY = date(2026, 9, 4)


class _Cur:
    def __init__(self, routes: list[Route]) -> None:
        self._routes = routes
        self._rows: list[tuple[Any, ...]] = []
        self.description: list[tuple[str]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        for route in self._routes:
            pred, rows = route[0], route[1]
            if pred(sql):
                self._rows = list(rows)
                self.description = [(c,) for c in (route[2] if len(route) > 2 else [])]
                return
        self._rows = []
        self.description = []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, routes: list[Route]) -> None:
        self.routes = routes

    def cursor(self) -> _Cur:
        return _Cur(self.routes)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def _lens_hit_routes() -> list[Route]:
    scoped = [("cold", True, True)] * 8 + [("cold", False, None)] * 4
    return [(lambda sql: "stock_signal_lens_hit_daily" in sql, scoped)]


def test_iv_rank_exhibit_carries_verdict_track_record_and_similar() -> None:
    routes: list[Route] = [
        (lambda sql: "option_metric_iv_percentile_daily" in sql and "ORDER BY trade_date DESC" in sql,
         [(TODAY, 16.0, 15.8, 0.34, NOW)]),
        # k-NN neighbours (similar_regime._similar_iv_rank), then the forward-return bars
        (lambda sql: "option_metric_iv_percentile_daily" in sql and "ABS(" in sql,
         [(date(2026, 7, 1), "NVDA", 16.8, 16.8, 15.8, 0.35), (date(2026, 8, 31), "NVDA", 17.8, 17.8, 8.8, 0.36)],
         ["trade_date", "symbol", "lens_value", "iv_rank_1y", "iv_percentile_1y", "iv_current"]),
        (lambda sql: "raw_market.stock_daily" in sql,
         [(date(2026, 7, 1), 100.0), (date(2026, 7, 2), 101.0), (date(2026, 7, 3), 102.0),
          (date(2026, 7, 6), 103.0), (date(2026, 7, 7), 104.0), (date(2026, 7, 8), 105.0)]),
        *_lens_hit_routes(),
    ]
    exh = build_exhibit(_Conn(routes), "iv_rank", "nvda")
    assert exh.lens == "iv_rank" and exh.lens_id == "iv_rank" and exh.symbol == "NVDA"
    assert exh.freshness == "fresh" and exh.readings["iv_rank_1y"] == 16.0
    assert exh.verdict is not None and exh.verdict["band"] == "cold"
    assert "long-premium" in exh.verdict["means"]
    assert exh.track_record is not None and exh.track_record["symbol_scoped"] is True
    assert exh.track_record["by_side"]["cold"]["hit_rate_5d"] == round(8 / 12, 4)
    assert exh.similar is not None and exh.similar["lens"] == "iv_rank"
    assert exh.similar["n"] == 2 and exh.similar["n_resolved"] == 2
    assert exh.similar["median_fwd"] == 0.05


def test_skew_reads_the_nearest_30dte_fit_and_judges_it_against_its_own_year() -> None:
    routes: list[Route] = [
        # C2: (days, avg |slope|, percentile of today's |slope| in the symbol's year)
        (lambda sql: "option_surface_fit_daily" in sql and "COUNT(*)" in sql, [(120, 0.09, 91.5)]),
        (lambda sql: "option_surface_fit_daily" in sql,
         [(TODAY, date(2026, 10, 2), 28, 0.41, -0.30, 0.004, 14, NOW)]),
    ]
    exh = build_exhibit(_Conn(routes), "skew", "NVDA")
    assert exh.readings["atm_slope"] == -0.30 and exh.readings["dte"] == 28
    assert exh.readings["slope_pctile_252d"] == 91.5 and exh.readings["history_days"] == 120
    assert exh.history_summary == {"days": 120, "avg_abs_atm_slope": 0.09}
    assert exh.verdict is not None and exh.verdict["band"] == "hot"
    assert exh.verdict["value"] == 91.5
    assert exh.track_record is None
    assert any("track record" in c for c in exh.caveats)


def test_skew_percentile_is_the_verdict_not_the_raw_slope() -> None:
    # The same -0.30 slope is calm for a name whose year ran steeper.
    routes: list[Route] = [
        (lambda sql: "option_surface_fit_daily" in sql and "COUNT(*)" in sql, [(200, 0.35, 42.0)]),
        (lambda sql: "option_surface_fit_daily" in sql,
         [(TODAY, date(2026, 10, 2), 28, 0.41, -0.30, 0.004, 14, NOW)]),
    ]
    exh = build_exhibit(_Conn(routes), "skew", "NVDA")
    assert exh.verdict is not None and exh.verdict["band"] == "neutral"
    thin: list[Route] = [
        (lambda sql: "option_surface_fit_daily" in sql and "COUNT(*)" in sql, [(12, 0.2, 100.0)]),
        (lambda sql: "option_surface_fit_daily" in sql,
         [(TODAY, date(2026, 10, 2), 28, 0.41, -0.30, 0.004, 14, NOW)]),
    ]
    exh = build_exhibit(_Conn(thin), "skew", "NVDA")
    assert any("12 history days" in c for c in exh.caveats)


def test_opex_pin_uses_close_on_the_max_pain_date(monkeypatch) -> None:
    from bifrost_research.lenses import exhibit_lenses

    monkeypatch.setattr(
        exhibit_lenses.opex_repo,
        "get_pin_analysis",
        lambda conn, symbol, *, cycles=24: [
            {"pct_distance": 0.002},
            {"pct_distance": -0.011},
            {"pct_distance": 0.004},
            {"pct_distance": None},
        ],
    )
    routes: list[Route] = [
        (lambda sql: "option_metric_max_pain_daily" in sql, [(TODAY, date(2026, 9, 18), 230.0, 500_000, NOW, 14)]),
        (lambda sql: "raw_market.stock_daily" in sql and "bar_date = %s" in sql, [(231.0,)]),
        (lambda sql: "raw_market.stock_daily" in sql, []),
        (lambda sql: "vanna_charm" in sql or "dte_to_opex" in sql, []),
        *_lens_hit_routes(),
    ]
    exh = build_exhibit(_Conn(routes), "opex_pin", "NVDA")
    assert exh.readings["max_pain_strike"] == 230.0 and exh.readings["close"] == 231.0
    assert abs(exh.readings["pin_pct_distance"] - (1.0 / 231.0)) < 1e-9
    # C2: the magnet's record rides along — 2 of 3 settled cycles within 0.5%.
    assert exh.history_summary["cycles"] == 3
    assert exh.history_summary["pinned"] == 2
    assert abs(exh.history_summary["pin_rate"] - 2 / 3) < 1e-9
    assert exh.verdict is not None and exh.verdict["band"] == "hot"
    assert exh.track_record is not None


def test_gex_regime_is_categorical_on_the_sign_of_net_gamma() -> None:
    routes: list[Route] = [
        (lambda sql: "option_metric_gex_levels_daily" in sql,
         [(TODAY, date(2026, 10, 2), 230.0, -5.0e8, 236.0, 240.0, 220.0, NOW)]),
    ]
    exh = build_exhibit(_Conn(routes), "gex_regime", "NVDA")
    assert exh.readings["regime"] == "negative" and exh.readings["spot_vs_zero_gamma"] == "below"
    assert exh.verdict is not None and exh.verdict["band"] == "hot"
    assert exh.similar is None  # categorical lenses carry no k-NN summary


def test_terrain_alias_answers_with_the_name_asked_for() -> None:
    routes: list[Route] = [
        (lambda sql: "stock_forecast_terrain_daily" in sql,
         [(TODAY, "range", 40.0, 74.7, 83.3, 7.0, 225.4, 230.36, NOW)]),
    ]
    exh = build_exhibit(_Conn(routes), "terrain", "NVDA")
    assert exh.lens == "terrain" and exh.lens_id == "terrain_regime"
    assert exh.verdict is not None and exh.verdict["band"] == "neutral"
    canonical = build_exhibit(_Conn(routes), "terrain_regime", "NVDA")
    assert canonical.lens == "terrain_regime" and canonical.readings == exh.readings


def test_order_sentiment_without_tape_carries_no_verdict() -> None:
    routes: list[Route] = [
        (lambda sql: "option_flow_sentiment_daily" in sql,
         [(TODAY, 0.0, 0.12, 0.9, 0.0, 0.0, NOW, "option_snapshot_aggregates")]),
    ]
    exh = build_exhibit(_Conn(routes), "order_sentiment", "SPY")
    assert exh.readings["data_source"] == "option_snapshot_aggregates"
    assert exh.verdict is None
    assert any("tape" in c for c in exh.caveats)
    taped = build_exhibit(
        _Conn([(lambda sql: "option_flow_sentiment_daily" in sql,
                [(TODAY, 45.0, 0.5, 0.9, 2e6, 1e6, NOW, "option_trades_tape")])]),
        "order_sentiment",
        "SPY",
    )
    assert taped.verdict is not None and taped.verdict["band"] == "hot"


def test_momentum_sepa_forecast_and_term_slope_readers() -> None:
    routes: list[Route] = [
        (lambda sql: "stock_signal_momentum_daily" in sql, [(TODAY, 85.2, "A+", "EXT", 72.2, 100.0, 100.0, 95.0, NOW)]),
        (lambda sql: "stock_signal_sepa_daily" in sql, [(TODAY, 81.7, "A", "STAGE_2A", "PIVOT", 62.5, 100.0, 70.0, 92.9, NOW)]),
        (lambda sql: "stock_backtest_settlement" in sql, [(15, 0.6, 0.0098, TODAY, NOW)]),
        (lambda sql: "option_surface_fit_daily" in sql,
         [(TODAY, date(2026, 9, 18), 14, 0.44, NOW), (TODAY, date(2026, 10, 2), 28, 0.41, NOW),
          (TODAY, date(2026, 12, 18), 105, 0.38, NOW)]),
    ]
    conn = _Conn(routes)
    mom = build_exhibit(conn, "momentum", "PLTR")
    assert mom.verdict is not None and mom.verdict["band"] == "hot" and mom.readings["grade"] == "A+"
    sepa = build_exhibit(conn, "sepa", "NVDA")
    assert sepa.verdict is not None and sepa.verdict["band"] == "hot" and sepa.readings["stage"] == "STAGE_2A"
    fc = build_exhibit(conn, "forecast_path", "SPY")
    assert fc.readings["session_count"] == 15 and fc.readings["path_hit_rate"] == 0.6 and fc.verdict is None
    ts = build_exhibit(conn, "term_slope", "NVDA")
    assert ts.readings["near_dte"] == 28 and ts.readings["far_dte"] == 105
    assert abs(ts.readings["term_slope"] - (0.38 - 0.41)) < 1e-9
    # C2: near − far of +3 vol points is backwardation, and the registry says so.
    assert abs(ts.readings["backwardation"] - 0.03) < 1e-9
    assert ts.readings["term_structure"] == "backwardation"
    assert ts.verdict is not None and ts.verdict["band"] == "hot"


def test_unknown_lens_and_the_registry_cover_each_other() -> None:
    with pytest.raises(ValueError, match="unknown lens"):
        build_exhibit(_Conn([]), "moon_phase", "NVDA")
    assert exhibit_lens_names() == set(LENSES) | set(LENS_ALIASES)
    for lens in LENSES:
        exh = build_exhibit(_Conn([]), lens, "NVDA")
        assert exh.lens_id == lens and exh.freshness == "missing"


@pytest.fixture(autouse=True)
def _patch_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bifrost_research.api.health.run_startup_schema_guard", lambda: None)
    import bifrost_research.api.health as health_mod

    health_mod._startup_ok = True
    health_mod._startup_error = None


def test_http_surface_accepts_every_lens_and_rejects_unknown() -> None:
    client = TestClient(create_app())
    with patch("bifrost_research.api.exhibit.connect", return_value=_Conn([])):
        res = client.get("/research/exhibit/gex_regime", params={"symbol": "nvda"})
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["lens"] == "gex_regime" and body["lens_id"] == "gex_regime" and body["verdict"] is None
        bad = client.get("/research/exhibit/moon_phase", params={"symbol": "NVDA"})
        assert bad.status_code == 400
        comp = client.get("/research/exhibit/composite", params={"symbol": "NVDA", "lenses": "skew,terrain,opex_pin"})
        assert comp.status_code == 200
        assert [e["lens"] for e in comp.json()["data"]["exhibits"]] == ["skew", "terrain", "opex_pin"]
