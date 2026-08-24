"""Wave 4.1 Market Terrain pure-compute tests."""

from __future__ import annotations

from datetime import date

from bifrost_research.engines.forecast.terrain import (
    classify_regime,
    compute_market_terrain,
    load_upstream_signals,
    pin_score_from_gex,
    trend_release_from_momentum,
    vol_squeeze_from_iv,
)


def test_pin_score_near_zero_gamma() -> None:
    score = pin_score_from_gex(
        spot=100.0,
        zero_gamma=100.5,
        call_wall=105.0,
        put_wall=95.0,
    )
    assert score > 60


def test_vol_squeeze_inverted() -> None:
    assert vol_squeeze_from_iv(iv_percentile=10.0) > vol_squeeze_from_iv(iv_percentile=90.0)


def test_trend_halt_suppresses() -> None:
    high = trend_release_from_momentum(score=80, path="EXT")
    low = trend_release_from_momentum(score=80, path="HALT")
    assert high > low


def test_regime_crash_risk() -> None:
    assert (
        classify_regime(pin_score=40, trend_release=40, vol_squeeze=40, tail_risk=80)
        == "crash-risk"
    )


def test_compute_terrain_range() -> None:
    t = compute_market_terrain(
        "SPY",
        date(2024, 6, 3),
        spot=500.0,
        gex={
            "zero_gamma": 499.0,
            "major_call_wall": 510.0,
            "major_put_wall": 490.0,
            "total_net_gex": 1e9,
        },
        momentum={"score": 55, "path": "PB", "crash": 70},
        iv={"iv_percentile_1y": 25.0},
    )
    assert t.symbol == "SPY"
    assert t.regime in {"crash-risk", "range", "trending"}
    assert 0 <= t.pin_score <= 100
    assert t.gamma_zone_low <= t.gamma_zone_high
    assert "advisory" in t.inputs_json


class _FakeTerrainCursor:
    """Cursor that returns queued rowsets in order (mirrors query sequence)."""

    def __init__(self, rowsets: list):
        self._rowsets = list(rowsets)
        self._current = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def execute(self, query: str, params=None) -> None:
        self._current = self._rowsets.pop(0) if self._rowsets else None

    def fetchone(self):
        cur = self._current
        if cur is None:
            return None
        if isinstance(cur, list):
            return cur[0] if cur else None
        return cur

    def fetchall(self):
        cur = self._current
        if cur is None:
            return []
        if isinstance(cur, list):
            return list(cur)
        return [cur]


class _FakeTerrainConn:
    def __init__(self, rowsets: list):
        self._cursor = _FakeTerrainCursor(rowsets)

    def cursor(self):
        return self._cursor

    def rollback(self):
        return None


def test_load_upstream_signals_uses_spot_fallback_for_index() -> None:
    """SPX case: gex_levels.spot is 0, no stock_daily row → fallback kicks in."""
    rowsets = [
        # gex_levels_daily: zero_gamma, major_call_wall, major_put_wall, total_net_gex, spot
        (7710.0, 7720.0, 7700.0, 1e9, 0.0),
        # momentum_score_daily: score, path, crash — none for SPX
        None,
        # iv_percentile_daily: iv_percentile_1y, iv_rank_1y — none
        None,
        # stock_daily.close — no row (SPX not tradable)
        None,
        # fetch_spot_fallback: option_snapshot delta≈0.5 call strikes
        [(7690.0, 0.499), (7710.0, 0.501), (7685.0, 0.4993)],
    ]
    conn = _FakeTerrainConn(rowsets)
    spot, gex, mom, iv = load_upstream_signals(conn, "SPX", date(2026, 8, 21))
    assert spot > 0
    assert 7680.0 <= spot <= 7720.0
    assert gex.get("spot") == spot
    assert mom == {}
    assert iv == {}


def test_load_upstream_signals_prefers_gex_spot_when_present() -> None:
    rowsets = [
        (100.5, 105.0, 95.0, 1e9, 500.25),
        None,
        None,
    ]
    conn = _FakeTerrainConn(rowsets)
    spot, gex, _mom, _iv = load_upstream_signals(conn, "SPY", date(2026, 8, 21))
    assert spot == 500.25
    assert gex["spot"] == 500.25
