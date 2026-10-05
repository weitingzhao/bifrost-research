"""W6 — server-side standard indicators, crossing signals and their forward stats."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from bifrost_research.engines.backtest.event_defs import EventDef
from bifrost_research.engines.backtest import event_query
from bifrost_research.engines.indicators import bollinger, ema, get_signal, macd, rsi, signal_dates, signal_mask
from bifrost_research.engines.indicators.stats import forward_stats


def _days(n: int, start: date = date(2024, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def test_ema_seeds_with_the_sma_like_the_chart() -> None:
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)
    assert out[4] == pytest.approx(0.5 * 5 + 0.5 * 3.0)


def test_rsi_is_100_on_a_straight_rise_and_wilder_after() -> None:
    closes = [float(i) for i in range(1, 20)]
    out = rsi(closes, 14)
    assert out[13] is None and out[14] == 100.0
    flat = rsi([10.0] * 20, 14)
    assert flat[14] == 50.0


def test_bollinger_uses_population_sigma() -> None:
    bb = bollinger([1, 2, 3], 3, 2.0)
    sd = math.sqrt(((1 - 2) ** 2 + 0 + (3 - 2) ** 2) / 3)
    assert bb["mid"][2] == pytest.approx(2.0)
    assert bb["upper"][2] == pytest.approx(2 + 2 * sd)
    assert bb["lower"][0] is None


def test_macd_lines_up_with_its_parts() -> None:
    closes = [100 + math.sin(i / 5) * 5 for i in range(80)]
    m = macd(closes)
    i = 60
    assert m["hist"][i] == pytest.approx(m["macd"][i] - m["signal"][i])
    assert m["macd"][i] == pytest.approx(ema(closes, 12)[i] - ema(closes, 26)[i])


def test_crossings_fire_once_on_the_crossing_session() -> None:
    # down for 30 sessions then up for 30: close crosses above EMA(5) once, after the turn
    closes = [100 - i for i in range(30)] + [70 + 2 * i for i in range(30)]
    mask = signal_mask(closes, "close_ema_cross_up", {"length": 5})
    hits = [i for i, h in enumerate(mask) if h]
    assert len(hits) == 1 and hits[0] >= 30
    assert not any(signal_mask(closes, "close_ema_cross_down", {"length": 5})[30:])


def test_rsi_cross_up_through_30_after_a_selloff() -> None:
    closes = [100.0] * 5 + [100 - 3 * i for i in range(1, 20)] + [43 + 2 * i for i in range(1, 15)]
    dates = _days(len(closes))
    hits = signal_dates(dates, closes, "rsi_cross_up")
    assert len(hits) == 1 and hits[0] > dates[23]


def test_unknown_signal_and_bad_params_are_refused() -> None:
    with pytest.raises(ValueError):
        get_signal("nope")
    with pytest.raises(ValueError):
        get_signal("ema_cross_up").params({"fast": 50, "slow": 20})
    with pytest.raises(ValueError):
        get_signal("macd_cross_up").params({"fast": 1.5})
    # unknown keys are ignored, given numbers override
    assert get_signal("rsi_cross_up").params({"level": "25", "symbols": ["X"]})["level"] == 25.0


def test_forward_stats_compares_signal_to_every_session() -> None:
    closes = [100 - i for i in range(30)] + [70 + 2 * i for i in range(30)]
    dates = _days(len(closes))
    out = forward_stats({"AAA": (dates, closes)}, "close_ema_cross_up", {"length": 5}, dates[0], dates[-1], horizons=(5,))
    h = out["by_horizon"]["5"]
    assert out["signals"] == 1 and h["signal"]["win_rate"] == 1.0
    assert h["baseline"]["n"] == len(closes) - 5
    assert h["win_rate_edge"] == pytest.approx(1.0 - h["baseline"]["win_rate"])
    assert out["recent"][0]["symbol"] == "AAA"


def test_indicator_signal_event_kind_resolves_per_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    closes = [100 - i for i in range(30)] + [70 + 2 * i for i in range(30)]
    dates = _days(len(closes))
    bars = [{"date": d, "close": c} for d, c in zip(dates, closes)]
    seen: list[str] = []

    def fake_load(conn, sym, start, end, *, warmup_sessions=0):  # noqa: ANN001
        seen.append(sym)
        return bars

    monkeypatch.setattr("bifrost_research.engines.indicators.bars.load_bars", fake_load)
    ed = EventDef.from_dict(
        {"kind": "indicator_signal", "params": {"signal": "close_ema_cross_up", "length": 5, "symbols": ["aaa", "BBB"]}}
    )
    res = event_query.resolve_events_between(None, ed, dates[0], dates[-1])
    assert seen == ["AAA", "BBB"]
    assert res.source == "indicator" and not res.errors
    assert {s for s, _ in res.events} == {"AAA", "BBB"} and len(res.events) == 2


def test_indicator_signal_needs_symbols() -> None:
    ed = EventDef.from_dict({"kind": "indicator_signal", "params": {"signal": "macd_cross_up"}})
    with pytest.raises(ValueError):
        event_query.resolve_events_between(None, ed, date(2024, 1, 1), date(2024, 6, 1))


def test_sim_refuses_entering_before_an_indicator_signal() -> None:
    from pydantic import ValidationError

    from bifrost_research.api.backtest_sim import SimBody

    ev = {"kind": "indicator_signal", "params": {"signal": "macd_cross_up"}}
    with pytest.raises(ValidationError):
        SimBody(symbols=["SPY"], entry_event=ev, entry_offset_sessions=-1)
    assert SimBody(symbols=["SPY"], entry_event=ev, entry_offset_sessions=1).entry_offset_sessions == 1
