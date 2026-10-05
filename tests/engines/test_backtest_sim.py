"""Bar-by-bar option simulator (P2, 0.170.0) on synthetic Black–Scholes chains."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

import pytest

from bifrost_research.engines.backtest.canonical_pnl import bs_price
from bifrost_research.engines.backtest.sim import SimConfig, run_sim
from bifrost_research.engines.backtest.sim.chain import ChainStore, OptBar
from bifrost_research.engines.backtest.sim.rules import fill_price, slippage

START = date(2025, 1, 6)  # a Monday


def _sessions(n: int) -> list[date]:
    out, d = [], START
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _store(
    spot_fn: Callable[[int], float],
    *,
    n: int = 120,
    iv: float = 0.30,
    drop: Callable[[str, date], bool] = lambda t, d: False,
) -> ChainStore:
    days = _sessions(n)
    spot = {d: spot_fn(i) for i, d in enumerate(days)}
    fridays = [d for d in days if d.weekday() == 4]
    bars: list[OptBar] = []
    for d in days:
        s = spot[d]
        for exp in fridays:
            dte = (exp - d).days
            if dte < 0 or dte > 90:
                continue
            t = max(dte, 0.5) / 365.0
            for k in range(60, 145, 5):
                for right in ("C", "P"):
                    ticker = f"O:X{exp:%y%m%d}{right}{k:08d}"
                    if drop(ticker, d):
                        continue
                    px = bs_price(s, float(k), t, iv, right=right)
                    if px < 0.01:
                        continue
                    bars.append(OptBar(ticker, exp, float(k), right, d, round(px, 4), None, 10))
    return ChainStore("X", spot, bars)


def _run(store: ChainStore, **kw: object):
    cfg = SimConfig(**{"structure": "short_put", "target_dte": 45, "entry_every_sessions": 200, **kw})
    days = store.sessions
    return run_sim(None, ["X"], days[0], days[60], cfg, stores={"X": store})


def test_slippage_tiers_and_fills_never_go_negative() -> None:
    assert slippage(0.2) == 0.03 and slippage(1.0) == 0.05 and slippage(20.0) == pytest.approx(0.30)
    assert fill_price(0.02, "sell") == 0.0
    assert fill_price(1.0, "buy") == pytest.approx(1.05)


def test_flat_market_short_put_takes_profit_near_its_delta() -> None:
    res = _run(_store(lambda i: 100.0))
    t = res.trades[0]
    leg = t["legs"][0]
    assert abs(leg["entry_delta"] + 0.20) < 0.06
    assert leg["strike"] < 100
    assert t["exit_reason"] == "profit_take"
    assert t["pnl"] > 0
    assert res.summary["n_trades"] == 1


def test_a_crash_stops_the_short_put_out() -> None:
    res = _run(_store(lambda i: 100.0 if i < 5 else 80.0))
    assert res.trades[0]["exit_reason"] == "stop"
    assert res.trades[0]["pnl"] < 0
    assert res.summary["worst_trade"] == res.trades[0]["pnl"]


def test_held_to_expiry_otm_keeps_the_credit_less_entry_costs() -> None:
    res = _run(_store(lambda i: 100.0), profit_take_pct=None, stop_loss_mult=None, dte_exit=None)
    t = res.trades[0]
    assert t["exit_reason"] == "expiry"
    assert t["exit_debit"] == 0.0
    assert t["pnl"] == pytest.approx(t["entry_credit"] - 0.65)
    assert t["exit_date"] == t["legs"][0]["expiry"]


def test_expiry_in_the_money_is_marked_as_assignment_and_settled_at_intrinsic() -> None:
    res = _run(_store(lambda i: 100.0 if i < 5 else 85.0), profit_take_pct=None, stop_loss_mult=None, dte_exit=None)
    t = res.trades[0]
    assert t["exit_reason"] == "expiry_itm"
    k = t["legs"][0]["strike"]
    assert t["exit_debit"] == pytest.approx((k - 85.0) * 100)


def test_dte_exit_closes_before_expiry() -> None:
    res = _run(_store(lambda i: 100.0), profit_take_pct=None, stop_loss_mult=None, dte_exit=21)
    t = res.trades[0]
    assert t["exit_reason"] == "dte_exit"
    assert (date.fromisoformat(t["legs"][0]["expiry"]) - date.fromisoformat(t["exit_date"])).days <= 21


def test_iron_condor_wings_sit_beyond_the_shorts_and_define_the_risk() -> None:
    res = _run(_store(lambda i: 100.0), structure="iron_condor", short_delta=0.20, wing_width_pct=0.10)
    legs = {lg["label"]: lg for lg in res.trades[0]["legs"]}
    assert legs["long call"]["strike"] > legs["short call"]["strike"] > 100
    assert legs["long put"]["strike"] < legs["short put"]["strike"] < 100
    assert len({lg["expiry"] for lg in legs.values()}) == 1
    t = res.trades[0]
    width = max(
        legs["long call"]["strike"] - legs["short call"]["strike"],
        legs["short put"]["strike"] - legs["long put"]["strike"],
    )
    assert t["max_loss"] == pytest.approx(width * 100 - t["entry_credit"])
    assert t["margin"] == t["max_loss"]


def test_a_leg_that_stops_printing_is_closed_as_stale() -> None:
    store0 = _store(lambda i: 100.0)
    first = _run(store0, profit_take_pct=None, stop_loss_mult=None, dte_exit=None).trades[0]
    ticker = first["legs"][0]["ticker"]
    gone_from = store0.sessions[3]
    store = _store(lambda i: 100.0, drop=lambda t, d: t == ticker and d >= gone_from)
    t = _run(store, profit_take_pct=None, stop_loss_mult=None, dte_exit=None, max_stale_sessions=3).trades[0]
    assert t["exit_reason"] == "stale"
    assert t["legs"][0]["stale_sessions"] == 3


def test_schedule_caps_open_positions_and_equity_tracks_every_session() -> None:
    store = _store(lambda i: 100.0)
    res = _run(store, entry_every_sessions=1, max_open_per_symbol=2, profit_take_pct=None, stop_loss_mult=None, dte_exit=None)
    assert max(e["open_positions"] for e in res.equity) == 2
    assert all(e["margin_used"] >= 0 for e in res.equity)
    assert res.equity[-1]["equity"] == pytest.approx(100_000 + res.summary["total_pnl"], abs=0.05)
    assert "avg_pnl_ci95" in res.summary and res.summary["sample_note"] == "noise"
    assert [t["seq"] for t in res.trades] == list(range(1, len(res.trades) + 1))


def test_unknown_structure_is_rejected() -> None:
    with pytest.raises(ValueError):
        run_sim(None, ["X"], START, START, SimConfig(structure="jade_lizard"), stores={})


class _LoadConn:
    def __init__(self) -> None:
        self.sql: list[tuple[str, tuple]] = []

    def cursor(self) -> "_LoadConn":
        return self

    def __enter__(self) -> "_LoadConn":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def rollback(self) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> None:
        q = " ".join(sql.split())
        self.sql.append((q, params))
        if "raw_market.stock_daily" in q:
            self._rows = [(START, 101.0), (START + timedelta(days=1), 102.0)]
        elif "raw_market.option_daily" in q:
            self._rows = [
                ("O:X250221P00095000", date(2025, 2, 21), 95.0, "P", START, 1.2, 1.15, 40),
                ("O:X250221P00095000", date(2025, 2, 21), 95.0, "put", START, 1.2, None, 0),  # bad right → kept as P
                ("O:X250221Q00095000", date(2025, 2, 21), 95.0, "Q", START, 1.2, None, 0),
            ]
        else:
            self._rows = [(START - timedelta(days=3), 4.31)]

    def fetchall(self) -> list[tuple]:
        return self._rows


def test_chain_store_load_reads_as_traded_spot_standard_contracts_and_rates() -> None:
    conn = _LoadConn()
    store = ChainStore.load(conn, " x ", START, START + timedelta(days=5), max_dte=45)
    stock_sql, stock_params = conn.sql[0]
    assert "COALESCE(close_unadjusted, close)" in stock_sql and stock_params[0] == "X"
    opt_sql, _ = conn.sql[1]
    assert "- 17) !~ '[0-9]$'" in opt_sql  # adjusted contracts stay out
    assert store.sessions == [START, START + timedelta(days=1)]
    assert [b.right for b in store.chain_on(START, "P")] == ["P", "P"]
    assert store.chain_on(START, "P")[0].price("vwap") == 1.15
    assert store.chain_on(START, "P")[1].price("vwap") == 1.2  # no vwap → close
    assert store.rate(START) == pytest.approx(0.0431)
    assert store.rate(START + timedelta(days=40)) == 0.0


def test_a_delisted_name_closes_its_open_position_as_delisted() -> None:
    # B7: the listing ends 20 sessions in, with a 45-DTE put still open.
    full = _store(lambda i: 100.0, n=80)
    cut = full.sessions[19]
    bars = [b for by_day in full._by_ticker.values() for b in by_day.values() if b.bar_date <= cut]
    store = ChainStore("X", {d: px for d, px in full.spot.items() if d <= cut}, bars)
    store.delisted_on = cut
    cfg = SimConfig(structure="short_put", target_dte=45, entry_every_sessions=200, profit_take_pct=None, stop_loss_mult=None, dte_exit=None)
    res = run_sim(None, ["X"], store.sessions[0], store.sessions[-1], cfg, stores={"X": store})
    assert [t["exit_reason"] for t in res.trades] == ["delisted"]
    assert res.trades[0]["exit_date"] == store.sessions[-1].isoformat()


def test_event_entry_opens_one_session_before_each_event() -> None:
    from bifrost_research.engines.backtest.sim.engine import _event_entries, _run_symbol

    store = _store(lambda i: 100.0, n=120)
    days = store.sessions
    events = [days[20], days[70] + timedelta(days=1)]  # the second falls between sessions
    assert _event_entries(days, events, -1) == {days[19], days[70]}
    cfg = SimConfig(structure="short_put", target_dte=45, entry_offset_sessions=-1)
    trades, _curve, _skips = _run_symbol(store, days[0], days[100], cfg, events=events)
    assert sorted(t["entry_date"] for t in trades) == [days[19].isoformat(), days[70].isoformat()]
