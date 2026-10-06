"""P1: Pine exits and Pine-level strikes in the option simulator (sim.pine + engine)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from bifrost_research.engines.backtest.sim import SimConfig, run_sim
from bifrost_research.engines.backtest.sim import engine as sim_engine
from bifrost_research.engines.backtest.sim import pine as sim_pine
from bifrost_research.engines.backtest.sim.engine import _run_symbol
from bifrost_research.engines.backtest.sim.pine import (
    PineOverlay,
    PineRunnerUnavailable,
    act_session,
    overlay_from_result,
    reverse_plot_exits,
    strategy_exits,
)
from bifrost_research.engines.pine import client
from tests.engines.test_backtest_sim import _sessions, _store

DAYS = _sessions(120)


def _cfg(**kw: Any) -> SimConfig:
    base: dict[str, Any] = {
        "structure": "short_put",
        "target_dte": 45,
        "entry_event": {"kind": "pine_signal", "params": {"script": "supertrend", "side": "buy"}},
        "entry_offset_sessions": 0,
        # hold to expiry unless something else closes it
        "profit_take_pct": None,
        "stop_loss_mult": None,
        "dte_exit": None,
    }
    base.update(kw)
    return SimConfig(**base)


# -- timing ------------------------------------------------------------------------


def test_an_exit_is_acted_on_the_first_session_that_opens_after_it_was_known() -> None:
    # A fill at the open was decided on the previous close: act that session.
    assert act_session(DAYS, DAYS[10], at_open=True) == DAYS[10]
    # A fill inside the session (a stop traded through): the next session.
    assert act_session(DAYS, DAYS[10], at_open=False) == DAYS[11]
    # A Friday intrabar fill acts on Monday.
    fri = next(d for d in DAYS if d.weekday() == 4)
    assert act_session(DAYS, fri, at_open=False).weekday() == 0
    # Past the last session: nothing to act on.
    assert act_session(DAYS, DAYS[-1], at_open=False) is None


def test_strategy_exits_by_direction_and_reverse_plots_the_session_after() -> None:
    trades = {
        "closed": [
            {"direction": "long", "exit_date": DAYS[12], "exit_at_open": True},
            {"direction": "long", "exit_date": DAYS[30], "exit_at_open": False},
            {"direction": "short", "exit_date": DAYS[40], "exit_at_open": True},
        ],
        "open": [{"direction": "long", "entry_date": DAYS[50]}],
    }
    assert strategy_exits(DAYS, trades) == {"long": [DAYS[12], DAYS[31]], "short": [DAYS[40]]}
    assert reverse_plot_exits(DAYS, buy=[DAYS[5]], sell=[DAYS[8], DAYS[9]]) == {
        "long": [DAYS[9], DAYS[10]],
        "short": [DAYS[6]],
    }


def test_overlay_level_is_the_value_before_the_entry_session() -> None:
    ov = PineOverlay(level={DAYS[20]: 92.0, DAYS[21]: 50.0, DAYS[22]: None})
    assert ov.level_before(DAYS[21]) == 92.0  # not the entry session's own value
    assert ov.level_before(DAYS[22]) == 50.0
    assert ov.level_before(DAYS[23]) is None  # na on the session before
    assert ov.level_before(DAYS[20]) is None
    ov = PineOverlay(exits={"long": [DAYS[10], DAYS[30]], "short": []})
    assert ov.exit_after("long", DAYS[10]) == DAYS[30]  # strictly after the entry
    assert ov.exit_after("long", DAYS[31]) is None
    assert ov.exit_after("short", DAYS[0]) is None


def test_auto_mode_uses_the_strategy_when_there_is_one_and_the_reverse_plot_otherwise() -> None:
    strat = {
        "buy": [DAYS[3]],
        "sell": [DAYS[7]],
        "trades": {"closed": [{"direction": "long", "exit_date": DAYS[20], "exit_at_open": True}], "open": []},
    }
    ov, used = overlay_from_result(DAYS, strat, exit_mode="auto", anchor_plot=None)
    assert used == "strategy" and ov.exits["long"] == [DAYS[20]]
    ind = {"buy": [DAYS[3]], "sell": [DAYS[7]], "trades": None, "series": {"Supertrend": {DAYS[2]: 95.0}}}
    ov, used = overlay_from_result(DAYS, ind, exit_mode="auto", anchor_plot="Supertrend")
    assert used == "reverse_plot" and ov.exits["long"] == [DAYS[8]]
    assert ov.level_before(DAYS[3]) == 95.0
    with pytest.raises(ValueError, match="indicator"):
        overlay_from_result(DAYS, ind, exit_mode="strategy", anchor_plot=None)
    # reverse_plot on a strategy uses its plots, not its trades
    ov, used = overlay_from_result(DAYS, strat, exit_mode="reverse_plot", anchor_plot=None)
    assert used == "reverse_plot" and ov.exits["long"] == [DAYS[8]]


# -- pine_exit in the session loop ---------------------------------------------------


def test_a_pine_exit_closes_the_position_on_its_act_session() -> None:
    store = _store(lambda i: 100.0)
    days = store.sessions
    ov = PineOverlay(exits={"long": [days[15], days[30]], "short": [days[25]]})
    trades, _c, _s = _run_symbol(store, days[0], days[100], _cfg(pine_exit="auto"), events=[days[20]], overlay=ov)
    t = trades[0]
    assert t["entry_date"] == days[21].isoformat()
    # days[15] is before the entry; days[25] closes shorts, not this long
    assert t["exit_reason"] == "pine_exit" and t["exit_date"] == days[30].isoformat()
    assert t["pine_exit_on"] == days[30].isoformat()
    leg = t["legs"][0]
    # filled at that session's own mark with the exit slippage
    from bifrost_research.engines.backtest.sim.rules import fill_price

    assert leg["exit_fill"] == pytest.approx(fill_price(store.bar(leg["ticker"], days[30]).price("vwap"), "buy"), abs=1e-4)
    # without pine_exit the same entry holds to expiry
    trades, _c, _s = _run_symbol(store, days[0], days[100], _cfg(), events=[days[20]], overlay=ov)
    assert trades[0]["exit_reason"] in ("expiry", "expiry_itm") and "pine_exit_on" not in trades[0]


def test_a_sell_entry_reads_the_short_exits() -> None:
    store = _store(lambda i: 100.0)
    days = store.sessions
    ov = PineOverlay(exits={"long": [days[30]], "short": [days[25]]})
    cfg = _cfg(pine_exit="auto", entry_event={"kind": "pine_signal", "params": {"script": "x", "side": "sell"}})
    trades, _c, _s = _run_symbol(store, days[0], days[100], cfg, events=[days[20]], overlay=ov)
    assert trades[0]["exit_date"] == days[25].isoformat() and trades[0]["exit_reason"] == "pine_exit"


def test_a_premium_rule_that_fires_first_wins() -> None:
    store = _store(lambda i: 100.0)
    days = store.sessions
    ov = PineOverlay(exits={"long": [days[80]], "short": []})
    trades, _c, _s = _run_symbol(
        store, days[0], days[100], _cfg(pine_exit="auto", profit_take_pct=0.5), events=[days[20]], overlay=ov
    )
    assert trades[0]["exit_reason"] == "profit_take"
    assert trades[0]["exit_date"] < days[80].isoformat()


def test_on_the_same_session_the_pine_exit_beats_a_stop_on_the_close() -> None:
    store = _store(lambda i: 100.0 if i < 30 else 80.0)
    days = store.sessions
    plain, _c, _s = _run_symbol(store, days[0], days[100], _cfg(stop_loss_mult=2.0), events=[days[20]])
    assert plain[0]["exit_reason"] == "stop" and plain[0]["exit_date"] == days[30].isoformat()
    ov = PineOverlay(exits={"long": [days[30]], "short": []})
    both, _c, _s = _run_symbol(
        store, days[0], days[100], _cfg(stop_loss_mult=2.0, pine_exit="auto"), events=[days[20]], overlay=ov
    )
    assert both[0]["exit_reason"] == "pine_exit" and both[0]["exit_date"] == days[30].isoformat()


def _patch_entries(monkeypatch: pytest.MonkeyPatch, events: dict[str, list[date]]) -> None:
    def fake_entry_rule(conn, symbols, start, end, cfg):  # noqa: ANN001
        return events, {"kind": "event", "anchor": "session_after_signal"}

    monkeypatch.setattr(sim_engine, "_entry_rule", fake_entry_rule)


def test_a_pine_exit_run_reports_the_premium_only_run_beside_it(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(lambda i: 100.0 - 0.15 * i)  # a slow slide: premium rules alone hold on
    days = store.sessions
    _patch_entries(monkeypatch, {"X": [days[5], days[25]]})
    ov = PineOverlay(exits={"long": [days[15], days[40]], "short": []})
    cfg = _cfg(pine_exit="auto", profit_take_pct=0.5, stop_loss_mult=2.0, dte_exit=21)
    res = run_sim(None, ["X"], days[0], days[60], cfg, stores={"X": store}, overlays={"X": ov})
    cmp = res.summary["pine_exit_comparison"]
    assert set(cmp) == {"premium_only", "with_pine_exit", "delta", "paired", "note"}
    assert cmp["with_pine_exit"]["exit_reasons"].get("pine_exit", 0) >= 1
    assert "pine_exit" not in cmp["premium_only"]["exit_reasons"]
    assert cmp["paired"]["n"] == 2 and cmp["paired"]["exits_changed"] >= 1
    assert cmp["with_pine_exit"]["total_pnl"] == res.summary["total_pnl"]
    assert cmp["delta"]["total_pnl"] == pytest.approx(
        cmp["with_pine_exit"]["total_pnl"] - cmp["premium_only"]["total_pnl"], abs=0.01
    )
    # the premium-only run is the same run without pine_exit
    base = run_sim(None, ["X"], days[0], days[60], _cfg(profit_take_pct=0.5, stop_loss_mult=2.0, dte_exit=21),
                   stores={"X": store}, overlays={"X": ov})
    assert cmp["premium_only"]["total_pnl"] == base.summary["total_pnl"]
    assert "pine_exit_comparison" not in base.summary
    assert res.params["pine_exit"] == "auto"


def test_a_name_the_runner_failed_on_is_skipped_not_run_without_its_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(lambda i: 100.0)
    days = store.sessions
    _patch_entries(monkeypatch, {"X": [days[5]]})
    res = run_sim(None, ["X"], days[0], days[60], _cfg(pine_exit="auto"), stores={"X": store}, overlays={})
    assert res.summary["n_trades"] == 0
    assert res.summary["skipped_entries"] == {"pine_unavailable": 1}
    assert res.summary["per_symbol"]["X"]["skipped"] == "pine_unavailable"


# -- strike_anchor -------------------------------------------------------------------


def test_the_short_put_goes_at_the_first_strike_at_or_below_the_level() -> None:
    store = _store(lambda i: 100.0)
    days = store.sessions
    ov = PineOverlay(level={days[20]: 92.0, days[21]: 50.0})
    cfg = _cfg(strike_anchor={"plot": "Supertrend", "min_delta": 0.05, "max_delta": 0.40})
    trades, _c, skips = _run_symbol(store, days[0], days[100], cfg, events=[days[20]], overlay=ov)
    leg = trades[0]["legs"][0]
    # strikes are 2.5 apart: 92.5 is above the line, 90 is the first below it
    assert leg["strike"] == 90.0 and leg["anchor_level"] == 92.0
    assert 0.05 <= abs(leg["entry_delta"]) <= 0.40
    assert skips == {}


def test_out_of_rails_missing_or_strikeless_levels_are_skipped_and_counted() -> None:
    store = _store(lambda i: 100.0)
    days = store.sessions
    cfg = _cfg(strike_anchor={"plot": "Supertrend", "min_delta": 0.10, "max_delta": 0.40})

    def skipped(level: float | None, c: SimConfig = cfg) -> dict[str, int]:
        ov = PineOverlay(level={days[20]: level})
        trades, _c, sk = _run_symbol(store, days[0], days[100], c, events=[days[20]], overlay=ov)
        assert trades == []
        return sk

    assert skipped(82.0) == {"anchor_delta_out_of_band": 1}  # far below: |delta| under the floor
    assert skipped(None) == {"anchor_missing": 1}
    assert skipped(55.0) == {"anchor_no_strike": 1}  # below the lowest listed strike
    # a level above spot would sell an in-the-money put: over the ceiling
    assert skipped(108.0) == {"anchor_delta_out_of_band": 1}


def test_a_put_credit_spread_anchors_the_short_and_keeps_the_wing_rule() -> None:
    store = _store(lambda i: 100.0)
    days = store.sessions
    ov = PineOverlay(level={days[20]: 92.0})
    cfg = _cfg(structure="put_credit_spread", strike_anchor={"plot": "Supertrend"})
    trades, _c, _s = _run_symbol(store, days[0], days[100], cfg, events=[days[20]], overlay=ov)
    short, wing = trades[0]["legs"]
    assert short["strike"] == 90.0 and short["anchor_level"] == 92.0
    assert wing["strike"] == 85.0 and "anchor_level" not in wing  # 5% of spot below the short


def test_pine_options_are_validated() -> None:
    store = _store(lambda i: 100.0)
    days = store.sessions
    run = lambda c: run_sim(None, ["X"], days[0], days[60], c, stores={"X": store}, overlays={})  # noqa: E731
    with pytest.raises(ValueError, match="pine_signal"):
        run(_cfg(pine_exit="auto", entry_event={"kind": "earnings"}))
    with pytest.raises(ValueError, match="one of"):
        run(_cfg(pine_exit="sometimes"))
    with pytest.raises(ValueError, match="needs plot"):
        run(_cfg(strike_anchor={"min_delta": 0.1}))
    with pytest.raises(ValueError, match="min_delta < max_delta"):
        run(_cfg(strike_anchor={"plot": "x", "min_delta": 0.4, "max_delta": 0.1}))
    with pytest.raises(ValueError, match="picks 2 legs"):
        run(_cfg(structure="short_strangle", strike_anchor={"plot": "x"}))


# -- loading overlays from the runner -------------------------------------------------


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def test_client_reads_series_and_trades_and_refuses_an_older_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
    sent: dict[str, Any] = {}

    def fake_post(path, payload, timeout):  # noqa: ANN001
        sent.update(payload)
        return {
            "ok": True,
            "results": [
                {
                    "symbol": "AAA",
                    "buy": [],
                    "sell": [],
                    "series": {"Supertrend": [[_ms(d1), None], [_ms(d2), 9.5]]},
                    "trades": {
                        "closed": [
                            {
                                "direction": "long",
                                "qty": 1,
                                "entry_time": _ms(d1),
                                "entry_price": 10,
                                "entry_at_open": True,
                                "exit_time": _ms(d2),
                                "exit_price": 11,
                                "exit_id": "SL",
                                "exit_comment": "SL",
                                "exit_at_open": False,
                                "profit": 1,
                            }
                        ],
                        "open": [],
                    },
                },
                {"symbol": "BBB", "buy": [], "sell": [], "series": {"Supertrend": []}, "trades": None},
            ],
        }

    monkeypatch.setattr(client, "_post", fake_post)
    bars = [{"date": d1, "close": 10.0}, {"date": d2, "close": 11.0}]
    out = client.run("src", {"AAA": bars, "BBB": bars}, plots=["Supertrend"], trades=True)
    assert sent["plots"] == ["Supertrend"] and sent["trades"] is True
    assert out["AAA"]["series"] == {"Supertrend": {d1: None, d2: 9.5}}
    t = out["AAA"]["trades"]["closed"][0]
    assert (t["entry_date"], t["exit_date"], t["exit_at_open"], t["exit_id"]) == (d1, d2, False, "SL")
    assert out["BBB"]["trades"] is None
    # a plain request sends neither option and reads neither key
    sent.clear()
    plain = client.run("src", {"AAA": bars})
    assert "plots" not in sent and "trades" not in sent and "series" not in plain["AAA"]

    monkeypatch.setattr(client, "_post", lambda *a, **k: {"ok": True, "results": [{"symbol": "AAA", "buy": [], "sell": []}]})
    with pytest.raises(ValueError, match="predates 0.2.0"):
        client.run("src", {"AAA": bars}, trades=True)


def test_load_overlays_builds_each_symbol_and_reports_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from bifrost_research.engines.pine import build, library
    from bifrost_research.engines.pine.library import PineScript

    bars = {s: [{"date": d, "close": 100.0} for d in DAYS[:60]] for s in ("AAA", "BBB")}
    monkeypatch.setattr(library, "get_script", lambda conn, sid: PineScript(id=sid, name=sid, source="src", version=4))
    monkeypatch.setattr(build, "load_bars_many", lambda conn, syms, start, end: {s: bars[s] for s in syms if s in bars})
    calls: list[dict[str, Any]] = []

    def fake_run(source, series, **kw):  # noqa: ANN001
        calls.append(kw)
        return {
            "AAA": {
                "buy": [DAYS[10]],
                "sell": [DAYS[20]],
                "warnings": [],
                "series": {"Supertrend": {DAYS[9]: 97.0}},
                "trades": None,
            },
            "BBB": {"error": "boom"},
        }

    monkeypatch.setattr(client, "run", fake_run)
    ovs, report = sim_pine.load_overlays(
        None, "supertrend", ["aaa", "BBB"], DAYS[0], DAYS[59], exit_mode="auto", anchor_plot="Supertrend"
    )
    assert calls == [{"plots": ["Supertrend"], "trades": True}]
    assert set(ovs) == {"AAA"} and ovs["AAA"].exits["long"] == [DAYS[21]]
    assert ovs["AAA"].level_before(DAYS[10]) == 97.0
    assert report["errors"] == {"BBB": "boom"} and report["exit_mode_used"] == "reverse_plot"
    assert report["script_version"] == 4 and report["per_symbol"]["AAA"]["exits_long"] == 1

    def down(*a, **k):  # noqa: ANN002, ANN003
        raise OSError("connection refused")

    monkeypatch.setattr(client, "run", down)
    with pytest.raises(PineRunnerUnavailable, match="connection refused"):
        sim_pine.load_overlays(None, "supertrend", ["AAA"], DAYS[0], DAYS[59], exit_mode="auto", anchor_plot=None)
