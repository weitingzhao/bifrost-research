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
            # 2.5-wide strikes: close enough that a 20-delta short sits within
            # the simulator's 0.05 delta tolerance.
            for k in (x / 2 for x in range(120, 290, 5)):
                for right in ("C", "P"):
                    ticker = f"O:X{exp:%y%m%d}{right}{int(k * 1000):08d}"
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


def _spread(structure: str, spot_fn: Callable[[int], float], **kw: object) -> dict:
    res = _run(_store(spot_fn), structure=structure, short_delta=0.20, wing_width_pct=0.05, **kw)
    return res.trades[0]


def test_call_credit_spread_sells_the_call_above_spot_and_buys_the_wing_above_it() -> None:
    t = _spread("call_credit_spread", lambda i: 100.0)
    legs = {lg["label"]: lg for lg in t["legs"]}
    assert set(legs) == {"short call", "long call"}
    assert legs["short call"]["side"] == "sell" and legs["long call"]["side"] == "buy"
    assert legs["short call"]["right"] == legs["long call"]["right"] == "C"
    assert legs["long call"]["strike"] > legs["short call"]["strike"] > 100
    assert legs["short call"]["expiry"] == legs["long call"]["expiry"]
    assert abs(legs["short call"]["entry_delta"] - 0.20) < 0.06
    assert legs["long call"]["entry_delta"] is None  # anchored, not picked by delta
    width = legs["long call"]["strike"] - legs["short call"]["strike"]
    assert t["entry_credit"] > 0
    assert t["max_loss"] == pytest.approx(width * 100 - t["entry_credit"])
    assert t["margin"] == t["max_loss"]


def test_call_and_put_credit_spreads_mirror_each_other() -> None:
    """Same delta, wing and rules: the strikes mirror around spot, risk is width
    minus credit on both, and each loses on the move the other wins on."""
    put = _spread("put_credit_spread", lambda i: 100.0)
    call = _spread("call_credit_spread", lambda i: 100.0)
    pl = {lg["label"]: lg for lg in put["legs"]}
    cl = {lg["label"]: lg for lg in call["legs"]}
    put_width = pl["short put"]["strike"] - pl["long put"]["strike"]
    call_width = cl["long call"]["strike"] - cl["short call"]["strike"]
    assert put_width == pytest.approx(call_width, abs=2.5)  # 5% of spot on a 2.5 grid
    assert 100 - pl["short put"]["strike"] == pytest.approx(cl["short call"]["strike"] - 100, abs=5.0)
    assert abs(pl["short put"]["entry_delta"]) == pytest.approx(cl["short call"]["entry_delta"], abs=0.06)
    for t, width in ((put, put_width), (call, call_width)):
        assert t["max_loss"] == pytest.approx(width * 100 - t["entry_credit"])
        assert t["margin"] == t["max_loss"]
    # Flat: both decay into a profit (which rule closes it first depends on the credit).
    for t in (put, call):
        assert t["exit_reason"] in ("profit_take", "dte_exit") and t["pnl"] > 0

    hold = {"profit_take_pct": None, "stop_loss_mult": None, "dte_exit": None}
    # Through both strikes at expiry: the full width is lost, the entry costs on top
    # (intrinsic settlement pays no exit commission).
    put_crash = _spread("put_credit_spread", lambda i: 100.0 if i < 5 else 70.0, **hold)
    call_rally = _spread("call_credit_spread", lambda i: 100.0 if i < 5 else 130.0, **hold)
    for t in (put_crash, call_rally):
        assert t["exit_reason"] == "expiry_itm"
        assert t["pnl"] == pytest.approx(-t["max_loss"] - 2 * 0.65)
    # The other way round both expire worthless and keep the credit. (The fixture
    # stops printing contracts under a cent, so staleness is off here.)
    put_rally = _spread("put_credit_spread", lambda i: 100.0 if i < 5 else 130.0, max_stale_sessions=None, **hold)
    call_crash = _spread("call_credit_spread", lambda i: 100.0 if i < 5 else 70.0, max_stale_sessions=None, **hold)
    for t in (put_rally, call_crash):
        assert t["exit_reason"] == "expiry"
        assert t["pnl"] == pytest.approx(t["entry_credit"] - 2 * 0.65)


def test_a_rally_stops_the_call_credit_spread_out() -> None:
    t = _spread("call_credit_spread", lambda i: 100.0 if i < 5 else 115.0, stop_loss_mult=1.0)
    assert t["exit_reason"] == "stop"
    assert t["pnl"] < 0
    assert -t["pnl"] < t["max_loss"] + 100  # inside the width plus exit costs


def test_a_leg_that_stops_printing_is_closed_as_stale() -> None:
    store0 = _store(lambda i: 100.0)
    first = _run(store0, profit_take_pct=None, stop_loss_mult=None, dte_exit=None).trades[0]
    ticker = first["legs"][0]["ticker"]
    gone_from = store0.sessions[3]
    store = _store(lambda i: 100.0, drop=lambda t, d: t == ticker and d >= gone_from)
    t = _run(store, profit_take_pct=None, stop_loss_mult=None, dte_exit=None, max_stale_sessions=3).trades[0]
    assert t["exit_reason"] == "stale"
    assert t["legs"][0]["stale_sessions"] == 3
    # None turns the rule off, like the other rules: the leg rides to expiry.
    t2 = _run(store, profit_take_pct=None, stop_loss_mult=None, dte_exit=None, max_stale_sessions=None).trades[0]
    assert t2["exit_reason"] in ("expiry", "expiry_itm")
    assert t2["legs"][0]["stale_sessions"] > 3


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
    opt_sql = next(q for q, _ in conn.sql if "raw_market.option_daily" in q)
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


# -- 0.175.0: entry timing, delta guard, snapshot fill -----------------------------


def test_a_signal_opens_on_the_session_after_it_other_events_on_the_session_itself() -> None:
    from bifrost_research.engines.backtest.sim.engine import _event_entries, _run_symbol

    store = _store(lambda i: 100.0, n=120)
    days = store.sessions
    # A Friday signal opens on Monday; a signal dated between sessions opens on the next one.
    assert _event_entries(days, [days[20]], 0, after_event=True) == {days[21]}
    assert _event_entries(days, [days[20]], 1, after_event=True) == {days[22]}
    assert days[19].weekday() == 4
    assert _event_entries(days, [days[19] + timedelta(days=1)], 0, after_event=True) == {days[20]}
    assert _event_entries(days, [days[20]], 0) == {days[20]}
    pine = SimConfig(structure="short_put", target_dte=45, entry_event={"kind": "pine_signal"}, entry_offset_sessions=0)
    trades, _c, _s = _run_symbol(store, days[0], days[100], pine, events=[days[20]])
    assert [t["entry_date"] for t in trades] == [days[21].isoformat()]
    leg = trades[0]["legs"][0]
    # Filled at the next session's own print, not the signal session's.
    assert leg["entry_mark"] == store.bar(leg["ticker"], days[21]).price("vwap")
    earn = SimConfig(structure="short_put", target_dte=45, entry_event={"kind": "earnings"}, entry_offset_sessions=0)
    trades, _c, _s = _run_symbol(store, days[0], days[100], earn, events=[days[20]])
    assert [t["entry_date"] for t in trades] == [days[20].isoformat()]


def test_a_run_says_which_entry_timing_it_used() -> None:
    res = _run(_store(lambda i: 100.0))
    assert res.summary["entry_timing"]["version"] == 3
    assert res.summary["entry_timing"]["anchor"] == "schedule"
    assert res.params["entry_timing_version"] == 3
    assert res.summary["snapshot_fill"]["bars_added"] == 0  # injected store, no conn


def test_an_entry_the_chain_has_no_strike_for_is_skipped_not_opened_off_target() -> None:
    # Only 85 and 100 strikes: the nearest to a 20-delta put is far off it.
    full = _store(lambda i: 100.0)
    bars = [b for by in full._by_ticker.values() for b in by.values() if b.strike in (85.0, 100.0)]
    store = ChainStore("X", full.spot, bars)
    res = _run(store)
    assert res.trades == [] and res.summary["skipped_entries"] == {"delta_off_target": 1}
    loose = _run(ChainStore("X", full.spot, bars), delta_tolerance=None)
    assert len(loose.trades) == 1


class _ChainConn:
    """Serves ``full``'s sessions and bars the way Golden Source does, recording each option read."""

    def __init__(self, full: ChainStore) -> None:
        self.full = full
        self.option_reads: list[tuple[date, date]] = []
        self._rows: list[tuple] = []

    def cursor(self) -> "_ChainConn":
        return self

    def __enter__(self) -> "_ChainConn":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def rollback(self) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> None:
        if "raw_market.stock_daily" in sql:
            lo, hi = params[-2], params[-1]
            self._rows = [(d, px) for d, px in self.full.spot.items() if lo <= d <= hi]
        elif "raw_market.option_daily" in sql:
            _sym, lo, hi = params
            self.option_reads.append((lo, hi))
            self._rows = [
                (b.ticker, b.expiry, b.strike, b.right, b.bar_date, b.close, b.vwap, b.volume)
                for by in self.full._by_ticker.values()
                for b in by.values()
                if lo <= b.bar_date <= hi
            ]
        else:
            self._rows = []

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


def _most_bars_in_any(full: ChainStore, span_days: int) -> int:
    per_day: dict[date, int] = {}
    for by in full._by_ticker.values():
        for d in by:
            per_day[d] = per_day.get(d, 0) + 1
    return max(
        sum(n for d, n in per_day.items() if lo <= d < lo + timedelta(days=span_days)) for lo in full.sessions
    )


def test_a_windowed_store_walks_the_same_trades_holding_one_span_of_bars() -> None:
    # SPY over a year was 744k bars held at once: 620 MB, and research-api's
    # 512Mi container OOM-killed (2026-10-06). The walk now holds one span.
    full = _store(lambda i: 100.0 - 0.15 * i, n=160)
    days = full.sessions
    cfg = SimConfig(structure="put_credit_spread", target_dte=45, entry_every_sessions=3, dte_exit=None)
    eager = run_sim(None, ["X"], days[0], days[100], cfg, stores={"X": full})
    conn = _ChainConn(full)
    windowed = ChainStore.load_windowed(conn, "X", days[0], days[100], max_dte=45, span_days=10)
    got = run_sim(None, ["X"], days[0], days[100], cfg, stores={"X": windowed})
    assert eager.summary["n_trades"] >= 10
    assert got.trades == eager.trades and got.equity == eager.equity
    stats = windowed.span_stats()
    assert stats is not None and stats["loads"] == len(conn.option_reads) >= 10
    assert stats["peak_bars"] <= _most_bars_in_any(full, 10)
    assert stats["peak_bars"] * 5 < full.resident_bars()
    # the walk moved forward only: each span read once, none overlapping
    assert all(a[1] < b[0] for a, b in zip(conn.option_reads, conn.option_reads[1:]))
    # dropped once the symbol is done, so ten symbols never sit in memory together
    assert windowed.resident_bars() == 0
    # and the API's path loads this way
    from bifrost_research.engines.backtest.sim.engine import _load_store

    assert _load_store(conn, "X", days[0], days[100], cfg).span_stats() is not None


def test_a_longer_window_does_not_cost_the_simulator_more_memory() -> None:
    import tracemalloc

    full = _store(lambda i: 100.0 - 0.05 * i, n=240)
    days = full.sessions
    cfg = SimConfig(structure="short_put", target_dte=45, entry_every_sessions=3, dte_exit=None)

    def peak(n: int, *, windowed: bool) -> int:
        tracemalloc.start()
        try:
            if windowed:
                store = ChainStore.load_windowed(_ChainConn(full), "X", days[0], days[n], max_dte=45)
                run_sim(None, ["X"], days[0], days[n], cfg, stores={"X": store})
            else:
                ChainStore.load(_ChainConn(full), "X", days[0], days[n], max_dte=45)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    short, long_ = peak(40, windowed=True), peak(160, windowed=True)
    # four times the window, the same span resident (eager: 27 MB → 52 MB here)
    assert long_ < short * 1.25
    assert long_ * 4 < peak(160, windowed=False)


def test_option_bars_carry_no_instance_dict() -> None:
    bar = OptBar("O:X250221P00095000", date(2025, 2, 21), 95.0, "P", START, 1.2, None, 10)
    assert not hasattr(bar, "__dict__")


class _SnapConn:
    """option_snapshot rows for a store whose option_daily kept only near-ATM strikes."""

    def __init__(self, bars: list[OptBar]) -> None:
        from datetime import datetime, timezone

        self._ts = lambda d: datetime(d.year, d.month, d.day, 20, 0, tzinfo=timezone.utc)  # 16:00 ET
        self.bars = bars
        self.fill_queries: list[tuple] = []
        self._rows: list[tuple] = []

    def cursor(self) -> "_SnapConn":
        return self

    def __enter__(self) -> "_SnapConn":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def rollback(self) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> None:
        if "min(snapshot_ts)" in sql:
            self._rows = [(self._ts(min(b.bar_date for b in self.bars)),)]
            return
        self.fill_queries.append(params)
        lo, hi = params[1], params[2]
        wanted = set(params[3]) if len(params) > 3 else None
        self._rows = [
            (b.ticker, self._ts(b.bar_date), b.close, b.vwap, b.volume)
            for b in self.bars
            if lo <= self._ts(b.bar_date) < hi and (wanted is None or b.ticker in wanted)
        ]

    def fetchone(self) -> tuple:
        return self._rows[0]

    def fetchall(self) -> list[tuple]:
        return self._rows


def test_the_snapshot_fills_the_strikes_option_daily_dropped_and_marks_the_held_leg() -> None:
    from bifrost_research.engines.backtest.sim.engine import _run_symbol

    full = _store(lambda i: 100.0, n=80)
    occ = lambda b: f"O:X{b.expiry:%y%m%d}{b.right}{int(b.strike * 1000):08d}"  # noqa: E731
    every = [
        OptBar(occ(b), b.expiry, b.strike, b.right, b.bar_date, b.close, b.vwap, b.volume)
        for by in full._by_ticker.values()
        for b in by.values()
    ]
    near_atm = [b for b in every if 95.0 <= b.strike <= 105.0]
    store = ChainStore("X", full.spot, near_atm)
    conn = _SnapConn(every)
    store.attach_snapshot_fill(conn, min_dte=7, max_dte=104)
    cfg = SimConfig(structure="short_put", target_dte=45, entry_every_sessions=200, profit_take_pct=None, stop_loss_mult=None, dte_exit=None)
    days = store.sessions
    trades, _c, skips = _run_symbol(store, days[0], days[10], cfg)
    assert skips == {}
    leg = trades[0]["legs"][0]
    assert leg["source"] == "snapshot" and leg["strike"] < 95.0
    assert abs(abs(leg["entry_delta"]) - 0.20) <= 0.05
    assert leg["stale_sessions"] == 0  # marked every session from the snapshot, not carried
    assert trades[0]["exit_reason"] in ("expiry", "expiry_itm")
    stats = store.fill_stats()
    assert stats["active"] and stats["bars_added"] > 0
    # One whole-chain read for the entry session, then held-contract reads only.
    assert sum(1 for p in conn.fill_queries if len(p) == 3) == 1


def test_no_snapshot_history_means_no_fill() -> None:
    class _Empty(_SnapConn):
        def execute(self, sql: str, params: tuple) -> None:
            self._rows = [(None,)]

    store = _store(lambda i: 100.0, n=20)
    store.attach_snapshot_fill(_Empty([]), min_dte=7, max_dte=104)
    assert store.fill_stats()["active"] is False


@pytest.mark.parametrize("kind", ["sepa_hit", "iv_percentile_threshold", "pine_signal", "indicator_signal"])
def test_a_signal_kind_with_a_negative_offset_is_refused_before_any_read(kind: str) -> None:
    cfg = SimConfig(structure="short_put", entry_event={"kind": kind}, entry_offset_sessions=-1)
    with pytest.raises(ValueError, match="0 = the session after the signal"):
        run_sim(None, ["X"], START, START + timedelta(days=30), cfg, stores={})


def test_a_sepa_hit_opens_on_the_session_after_it() -> None:
    from bifrost_research.engines.backtest.sim.engine import _run_symbol

    store = _store(lambda i: 100.0, n=120)
    days = store.sessions
    cfg = SimConfig(structure="short_put", target_dte=45, entry_event={"kind": "sepa_hit"}, entry_offset_sessions=0)
    trades, _c, _s = _run_symbol(store, days[0], days[100], cfg, events=[days[20]])
    assert [t["entry_date"] for t in trades] == [days[21].isoformat()]
