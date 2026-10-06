"""Liquidity (S4, Owner 2026-10-06 option c): a volume gate at issue, a bounded entry delay at settlement."""

from __future__ import annotations

from datetime import date, timedelta

from bifrost_research.engines.backtest.canonical_pnl import bs_price
from bifrost_research.engines.backtest.sim.chain import ChainStore, OptBar
from bifrost_research.engines.backtest.sim.walk import LegSpec
from bifrost_research.engines.suggestion.config import BASELINE, LIQUIDITY, SETTLEMENT_METHOD_VERSION
from bifrost_research.engines.suggestion.contract import Suggestion
from bifrost_research.engines.suggestion.issue import build_suggestion
from bifrost_research.engines.suggestion.settle import _walk, tradeable_entry

START = date(2025, 1, 6)


def _days(n: int) -> list[date]:
    out, d = [], START
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _chain(volume: int) -> ChainStore:
    days = _days(60)
    fridays = [d for d in days if d.weekday() == 4]
    bars = []
    for d in days[:1]:
        for exp in fridays:
            dte = (exp - d).days
            if 30 <= dte <= 70:
                for k in range(60, 140, 1):
                    px = bs_price(100.0, float(k), dte / 365.0, 0.30, right="P", rate=0.0)
                    if px >= 0.01:
                        bars.append(OptBar(f"O:X{exp:%y%m%d}P{k * 1000:08d}", exp, float(k), "P", d, round(px, 4), None, volume))
    return ChainStore("X", {d: 100.0 for d in days}, bars)


def test_issue_refuses_a_leg_that_barely_traded() -> None:
    spec = {**BASELINE, "symbol": "X", "source_ref": "x"}
    assert build_suggestion(_chain(LIQUIDITY["min_leg_volume"] - 1), START, source="pine", spec=spec, regime={}) == "illiquid_leg"
    got = build_suggestion(_chain(LIQUIDITY["min_leg_volume"]), START, source="pine", spec=spec, regime={})
    assert isinstance(got, Suggestion)
    assert got.snapshot["liquidity"] == {"min_leg_volume": LIQUIDITY["min_leg_volume"]}


def test_the_etf_sources_are_not_gated() -> None:
    """Replay 2024-11..2026-10: the gate cost baseline 20 of 101 weeks and voided nothing without it."""
    assert set(LIQUIDITY["exempt_sources"]) == {"baseline", "simulator"}
    spec = {**BASELINE, "symbol": "X"}
    for source in ("baseline", "simulator"):
        got = build_suggestion(_chain(1), START, source=source, spec=spec, regime={})
        assert isinstance(got, Suggestion)
        assert got.snapshot["liquidity"] == {"min_leg_volume": 0}


LEG = LegSpec("O:X250221P00090000", "P", "sell", 90.0, date(2025, 2, 21), 1, "short put")


def _store(printed_on: list[int], n_after: int = 6) -> ChainStore:
    days = _days(1 + n_after)
    bars = [OptBar(LEG.ticker, LEG.expiry, 90.0, "P", days[i], 1.0, 1.0, 100) for i in printed_on]
    return ChainStore("X", {d: 100.0 for d in days}, bars)


def test_entry_waits_for_the_first_session_every_leg_trades() -> None:
    st = _store([0, 3, 4, 5, 6])  # no print on the first two sessions after the as-of
    entry, late, void = tradeable_entry(st, [LEG], st.sessions[0])
    assert (entry, late, void) == (st.sessions[3], 2, None)


def test_past_the_delay_it_is_a_void_and_before_that_it_stays_open() -> None:
    delay = LIQUIDITY["max_entry_delay"]
    st = _store([0], n_after=delay + 1)
    assert tradeable_entry(st, [LEG], st.sessions[0]) == (None, None, f"no_entry_bar_within_{delay}")
    young = _store([0], n_after=delay)  # the last allowed session has not come yet
    assert tradeable_entry(young, [LEG], young.sessions[0]) == (None, None, None)


def test_the_walk_records_how_late_it_entered() -> None:
    st = _store([0, 2, 3, 4, 5, 6])
    row = {"as_of_session": st.sessions[0], "structure": "short_put", "take_profit_pct": 0.5,
           "stop_loss_mult": None, "exit_dte": None, "max_hold_days": None}
    out, entry, _rules, extra = _walk(st, [LEG], row, "model")
    assert entry == st.sessions[2] and extra == {"entry_delay_sessions": 1}
    assert out.status in ("settled", "open")


def test_the_method_version_moved_with_the_entry_rule() -> None:
    assert SETTLEMENT_METHOD_VERSION == "walk-2"
