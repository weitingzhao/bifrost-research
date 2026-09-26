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


# ─── research-loop-automation A4: a gamma zone is never a point ───

from bifrost_research.engines.forecast.terrain import (  # noqa: E402
    expected_close_and_gamma_zone,
    gamma_zone_source,
)


def test_walls_on_one_strike_are_widened_and_named() -> None:
    _, low, high = expected_close_and_gamma_zone(
        spot=230.36, zero_gamma=236.0, call_wall=230.0, put_wall=230.0, regime="range", trend_release=50.0
    )
    assert low < high
    assert abs((high - low) - 230.0 * 0.01) < 1e-6
    assert gamma_zone_source(spot=230.36, call_wall=230.0, put_wall=230.0) == "walls_widened"
    assert gamma_zone_source(spot=230.36, call_wall=240.0, put_wall=220.0) == "walls"
    assert gamma_zone_source(spot=230.36, call_wall=None, put_wall=None) == "spot_band"
    assert gamma_zone_source(spot=230.36, call_wall=240.0, put_wall=None) == "one_wall_spot_band"


def test_compute_terrain_records_the_zone_source_and_keeps_a_readable_band() -> None:
    t = compute_market_terrain(
        "NVDA",
        date(2026, 9, 4),
        spot=230.36,
        gex={"zero_gamma": 236.0, "major_call_wall": 230.0, "major_put_wall": 230.0, "total_net_gex": -5e8},
        momentum={"score": 60, "path": "EXT", "crash": 40},
        iv={"iv_percentile_1y": 16.0},
    )
    assert t.gamma_zone_low < t.gamma_zone_high
    assert t.inputs_json["gamma_zone_source"] == "walls_widened"
    plain = compute_market_terrain("NVDA", date(2026, 9, 4), spot=230.36, gex=None, momentum=None, iv=None)
    assert plain.inputs_json["gamma_zone_source"] == "spot_band"
    assert plain.gamma_zone_low < plain.gamma_zone_high


def test_load_upstream_signals_takes_the_days_own_close_over_a_stale_levels_spot() -> None:
    """PLTR 09-10 read 174.33 from an older levels row; the day's close was 165.86."""
    rowsets = [
        (160.0, 180.0, 170.0, 1e8, 174.33),   # levels: newest row at or before the day
        None,                                   # momentum
        None,                                   # iv percentile
        (date(2026, 9, 10), 165.86),            # stock_daily: the day's own close
    ]
    spot, _gex, _m, _iv = load_upstream_signals(_FakeTerrainConn(rowsets), "PLTR", date(2026, 9, 10))
    assert spot == 165.86


def test_load_upstream_signals_keeps_the_levels_spot_over_an_older_close() -> None:
    """Intraday asks for today before today's bar exists: yesterday's close must not win."""
    rowsets = [
        (160.0, 180.0, 170.0, 1e8, 190.10),
        None,
        None,
        (date(2026, 9, 24), 192.59),            # stock_daily: the prior day only
    ]
    spot, _gex, _m, _iv = load_upstream_signals(_FakeTerrainConn(rowsets), "PLTR", date(2026, 9, 25))
    assert spot == 190.10


# ─── 2026-09-26: walls that never reached the price bound nothing ───

from bifrost_research.engines.forecast.terrain import (
    terrain_input_fault,
    walls_reach_spot,
)


def test_walls_off_spot_fall_back_to_the_spot_band() -> None:
    # PLTR 2026-07-27: the backfilled chain put every level on 20 against 131.53,
    # and the range target was drawn to 20.5.
    t = compute_market_terrain(
        "PLTR",
        date(2026, 7, 27),
        spot=131.53,
        gex={"zero_gamma": 20.0, "major_call_wall": 20.0, "major_put_wall": 20.0, "total_net_gex": 1e6},
        momentum={"score": 50, "path": "", "crash": 40},
        iv={"iv_percentile_1y": 50.0},
    )
    assert t.inputs_json["gamma_zone_source"] == "walls_off_spot"
    assert t.inputs_json["levels_off_spot"] == {
        "zero_gamma": 20.0,
        "major_call_wall": 20.0,
        "major_put_wall": 20.0,
    }
    assert t.gamma_zone_low < 131.53 < t.gamma_zone_high
    assert abs(t.expected_close / 131.53 - 1) < 0.02
    # The row it writes is not a fault: its forecast used the spot band.
    assert terrain_input_fault(t.spot, t.inputs_json) is None


def test_one_wall_near_spot_keeps_the_walls() -> None:
    assert walls_reach_spot(spot=17.1, call_wall=22.5, put_wall=17.0)
    assert walls_reach_spot(spot=100.0, call_wall=None, put_wall=None)
    assert not walls_reach_spot(spot=31.41, call_wall=0.5, put_wall=1.0)
    assert not walls_reach_spot(spot=100.0, call_wall=None, put_wall=70.0)
    t = compute_market_terrain(
        "NNE",
        date(2026, 9, 21),
        spot=17.1,
        gex={"zero_gamma": 22.15, "major_call_wall": 22.5, "major_put_wall": 17.0},
        momentum=None,
        iv=None,
    )
    assert t.inputs_json["gamma_zone_source"] == "walls"
    assert "levels_off_spot" not in t.inputs_json


def test_terrain_input_fault_names_rows_written_before_the_guard() -> None:
    old = {"gamma_zone_source": "walls_widened", "gex": {"major_call_wall": 20.0, "major_put_wall": 20.0}}
    assert terrain_input_fault(131.53, old) == "walls_off_spot"
    near = {"gamma_zone_source": "walls", "gex": {"major_call_wall": 135.0, "major_put_wall": 125.0}}
    assert terrain_input_fault(131.53, near) is None
    assert terrain_input_fault(131.53, {"gex": {}}) is None
    assert terrain_input_fault(131.53, None) is None
    assert terrain_input_fault(None, old) is None
