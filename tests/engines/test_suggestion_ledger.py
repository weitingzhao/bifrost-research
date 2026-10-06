"""Suggestion ledger (stage 2): contract, mechanical issue, walk-based settlement."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

import pytest

from bifrost_research.engines.backtest.canonical_pnl import bs_price
from bifrost_research.engines.backtest.sim import SimConfig, run_sim
from bifrost_research.engines.backtest.sim.chain import ChainStore, OptBar
from bifrost_research.engines.backtest.sim.walk import LegSpec, WalkRules, legs_from_json, walk_legs
from bifrost_research.engines.suggestion.config import BASELINE, THRESHOLDS
from bifrost_research.engines.suggestion.contract import IncompleteSuggestion, Suggestion
from bifrost_research.engines.suggestion.issue import build_suggestion, cadence_sessions
from bifrost_research.engines.suggestion.settle import entry_session, rules_for, settlement_row
from bifrost_research.schema.suggestion_ledger_ddl import ledger_statements, revoke_statements

START = date(2025, 1, 6)  # a Monday


def _sessions(n: int) -> list[date]:
    out, d = [], START
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _store(spot_fn: Callable[[int], float], *, n: int = 120, iv: float = 0.30) -> ChainStore:
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
                    px = bs_price(s, float(k), t, iv, right=right, rate=0.0)
                    if px < 0.01:
                        continue
                    bars.append(OptBar(f"O:X{exp:%y%m%d}{right}{k:08d}", exp, float(k), right, d, round(px, 4), None, 100))
    return ChainStore("X", spot, bars)


def _baseline_spec(**kw: object) -> dict:
    return {**BASELINE, "symbol": "X", **kw}


# -- contract -------------------------------------------------------------------------


def _suggestion(**kw: object) -> Suggestion:
    base = dict(
        as_of_session=date(2026, 10, 5),
        source="baseline",
        source_ref="spy_weekly_30d_put",
        source_version="1",
        symbol="spy",
        kind="option_structure",
        structure="short_put",
        legs=(
            {
                "contract_key": "O:SPY261120P00650000",
                "right": "P",
                "side": "sell",
                "strike": 650.0,
                "expiry": "2026-11-20",
                "ratio": 1,
            },
        ),
        take_profit_pct=0.5,
        exit_dte=21,
        snapshot={"spot": 700.0},
    )
    base.update(kw)
    return Suggestion(**base)  # type: ignore[arg-type]


def test_identity_is_stable_and_keyed_on_source_session_symbol() -> None:
    a, b = _suggestion(), _suggestion(rationale="different words")
    assert a.issue_key == "baseline:spy_weekly_30d_put@1:2026-10-05:SPY"
    assert _suggestion(source_version="2").issue_key != a.issue_key
    assert a.suggestion_id == b.suggestion_id and a.suggestion_id.startswith("sg_")
    assert a.inputs_hash == b.inputs_hash
    assert _suggestion(take_profit_pct=0.6).inputs_hash != a.inputs_hash
    row = a.validate().to_row()
    assert row["symbol"] == "SPY" and row["legs_json"].startswith("[")


@pytest.mark.parametrize(
    "kw, msg",
    [
        ({"source": "oracle"}, "source"),
        ({"kind": "hunch"}, "kind"),
        ({"structure": None}, "structure"),
        ({"legs": ()}, "legs"),
        ({"legs": ({"contract_key": "O:X", "right": None, "side": "sell", "strike": 1, "expiry": "2026-11-20", "ratio": 1},)}, "missing"),
        ({"legs": ({"contract_key": "O:X", "right": "P", "side": "sell", "strike": 1, "expiry": "2026-10-05", "ratio": 1},)}, "expires"),
        ({"take_profit_pct": None, "exit_dte": None}, "management rule"),
        ({"conviction": 9}, "conviction"),
    ],
)
def test_incomplete_suggestions_are_refused(kw: dict, msg: str) -> None:
    with pytest.raises(IncompleteSuggestion, match=msg):
        _suggestion(**kw).validate()


def test_stand_aside_needs_no_legs() -> None:
    _suggestion(kind="stand_aside", structure=None, legs=()).validate()


def test_thresholds_are_versioned() -> None:
    assert THRESHOLDS["version"] and THRESHOLDS["sample"]["min_settled"] == 100
    assert THRESHOLDS["expectancy"]["slippage_scale"] == 1.5


# -- DDL --------------------------------------------------------------------------------


def test_ledger_ddl_is_append_only_and_revokes_only_from_bifrost() -> None:
    sql = "\n".join(ledger_statements())
    for t in ("suggestion", "suggestion_settlement", "suggestion_adoption"):
        assert f"CREATE TABLE IF NOT EXISTS research.{t} (" in sql
        assert f"BEFORE UPDATE OR DELETE ON research.{t}" in sql
        assert f"BEFORE TRUNCATE ON research.{t}" in sql
    assert "DROP" not in sql.upper()
    # The owner keeps UPDATE: FK checks lock FOR KEY SHARE as the owner.
    assert all(s.endswith("FROM bifrost") for s in revoke_statements())


# -- issue ------------------------------------------------------------------------------


def test_weekly_cadence_is_the_first_session_of_each_iso_week() -> None:
    days = _sessions(15)
    # Drop Monday of week 2: Tuesday becomes that week's first session.
    days = [d for d in days if d != date(2025, 1, 13)]
    assert cadence_sessions(days, "weekly") == [date(2025, 1, 6), date(2025, 1, 14), date(2025, 1, 20)]


def test_baseline_suggestion_picks_a_30_delta_put_near_45_dte() -> None:
    store = _store(lambda i: 100.0)
    got = build_suggestion(store, START, source="baseline", spec=_baseline_spec(), regime={})
    assert isinstance(got, Suggestion)
    got.validate()
    leg = got.legs[0]
    assert leg["right"] == "P" and leg["side"] == "sell" and leg["strike"] < 100
    assert abs(abs(got.snapshot["legs"][0]["delta"]) - 0.30) < 0.06
    assert 30 <= got.snapshot["picked_dte"] <= 60
    assert got.expected_credit and got.expected_credit > 0
    assert got.exit_dte == 21 and got.take_profit_pct == 0.5 and got.stop_loss_mult is None


# -- walk / settle ----------------------------------------------------------------------


def _legs_of(s: Suggestion) -> list[LegSpec]:
    return legs_from_json(s.legs)


def test_walk_enters_next_session_and_takes_profit_in_a_flat_market() -> None:
    store = _store(lambda i: 100.0)
    s = build_suggestion(store, START, source="baseline", spec=_baseline_spec(), regime={})
    assert isinstance(s, Suggestion)
    entry = entry_session(store.sessions, s.as_of_session)
    assert entry == START + timedelta(days=1)
    out = walk_legs(store, _legs_of(s), entry, rules_for(s.__dict__, "model"), structure="short_put")
    assert out.status == "settled" and out.reason == "profit_take"
    assert out.trade["entry_date"] == entry.isoformat()
    assert out.trade["pnl"] > 0
    # gross = net + slippage + commission
    assert out.gross_pnl == pytest.approx(out.trade["pnl"] + out.entry_slippage + out.exit_slippage + out.commission)


def test_stress_slippage_costs_more() -> None:
    store = _store(lambda i: 100.0)
    s = build_suggestion(store, START, source="baseline", spec=_baseline_spec(), regime={})
    assert isinstance(s, Suggestion)
    entry = entry_session(store.sessions, s.as_of_session)
    a = walk_legs(store, _legs_of(s), entry, rules_for(s.__dict__, "model"), structure="short_put")
    b = walk_legs(store, _legs_of(s), entry, rules_for(s.__dict__, "model_stress"), structure="short_put")
    assert b.trade["pnl"] < a.trade["pnl"]
    assert b.entry_slippage == pytest.approx(1.5 * a.entry_slippage)


def test_walk_matches_the_simulator_on_the_same_legs() -> None:
    """A walk of the simulator's own legs from its entry session reproduces its trade."""
    store = _store(lambda i: 100.0 - 0.15 * i)
    cfg = SimConfig(structure="short_put", target_dte=45, entry_every_sessions=200, short_delta=0.3)
    res = run_sim(None, ["X"], store.sessions[0], store.sessions[60], cfg, stores={"X": store})
    t = res.trades[0]
    legs = [
        LegSpec(lg["ticker"], lg["right"], lg["side"], lg["strike"], date.fromisoformat(lg["expiry"]), lg["qty"])
        for lg in t["legs"]
    ]
    rules = WalkRules(
        profit_take_pct=cfg.profit_take_pct,
        stop_loss_mult=cfg.stop_loss_mult,
        dte_exit=cfg.dte_exit,
        max_stale_sessions=cfg.max_stale_sessions,
    )
    out = walk_legs(store, legs, date.fromisoformat(t["entry_date"]), rules, structure="short_put")
    assert out.status == "settled"
    assert out.trade["exit_date"] == t["exit_date"] and out.trade["exit_reason"] == t["exit_reason"]
    assert out.trade["pnl"] == pytest.approx(t["pnl"])


def test_walk_void_without_an_entry_print_and_open_when_data_ends() -> None:
    full = _store(lambda i: 100.0)
    s = build_suggestion(full, START, source="baseline", spec=_baseline_spec(), regime={})
    assert isinstance(s, Suggestion)
    legs = _legs_of(s)
    # The same chain, but the data stops ten sessions in — well before expiry.
    cut = full.sessions[9]
    store = ChainStore(
        "X",
        {d: v for d, v in full.spot.items() if d <= cut},
        [b for t in full._by_ticker.values() for d, b in t.items() if d <= cut],
    )
    rules = WalkRules(profit_take_pct=None, dte_exit=None, max_stale_sessions=None)
    out = walk_legs(store, legs, store.sessions[1], rules, structure="short_put")
    assert out.status == "open"
    ghost = [LegSpec("O:NOPE", "P", "sell", 90.0, legs[0].expiry)]
    assert walk_legs(store, ghost, store.sessions[1], rules).reason == "no_entry_bar:O:NOPE"


def test_a_debit_structure_takes_profit_on_its_debit() -> None:
    """Bought premium: profit take and stop measure against the debit, not a credit."""
    store = _store(lambda i: 100.0 + 0.8 * i)
    exp = next(d for d in store.sessions if d.weekday() == 4 and (d - START).days >= 40)
    leg = LegSpec(f"O:X{exp:%y%m%d}C{100:08d}", "C", "buy", 100.0, exp)
    out = walk_legs(store, [leg], store.sessions[1], WalkRules(profit_take_pct=0.5, stop_loss_mult=0.5, dte_exit=None))
    assert out.status == "settled" and out.reason == "profit_take"
    assert out.trade["pnl"] > 0


def test_settlement_row_uses_max_loss_or_margin_as_risk() -> None:
    store = _store(lambda i: 100.0)
    s = build_suggestion(store, START, source="baseline", spec=_baseline_spec(), regime={})
    assert isinstance(s, Suggestion)
    entry = entry_session(store.sessions, s.as_of_session)
    rules = rules_for(s.__dict__, "model")
    out = walk_legs(store, _legs_of(s), entry, rules, structure="short_put")
    row = settlement_row(s.suggestion_id, "model", out, entry=entry, rules=rules)
    assert row["status"] == "settled" and row["max_loss"] is None
    assert row["risk_basis"] == row["margin"] > 0
    assert row["return_on_risk"] == pytest.approx(row["net_pnl"] / row["risk_basis"], rel=1e-5)
    void = settlement_row(s.suggestion_id, "model", walk_legs(store, [], entry, rules), entry=entry, rules=rules)
    assert void["status"] == "void" and void["void_reason"] == "no_legs" and "net_pnl" not in void


# -- 0.174.1: delta guard, snapshot day bars ---------------------------------------------


def test_a_rule_whose_delta_the_chain_lacks_is_not_issued() -> None:
    days = _sessions(60)
    exp = next(d for d in days if d.weekday() == 4 and (d - START).days >= 40)
    # Only near-the-money strikes, like option_daily since mid-August 2026.
    bars = [
        OptBar(f"O:X{exp:%y%m%d}P{k:08d}", exp, float(k), "P", START, round(bs_price(100.0, float(k), (exp - START).days / 365, 0.3, right="P", rate=0.0), 4), None, 100)
        for k in (98, 99, 100, 101, 102)
    ]
    store = ChainStore("X", {d: 100.0 for d in days}, bars)
    assert build_suggestion(store, START, source="baseline", spec=_baseline_spec(), regime={}) == "delta_out_of_band"


def test_occ_tickers_parse() -> None:
    from bifrost_research.engines.backtest.sim.walk import _parse_occ

    assert _parse_occ("O:SPY261120P00765000") == (date(2026, 11, 20), "P", 765.0)
    assert _parse_occ("O:BRK.B261120C00412500") == (date(2026, 11, 20), "C", 412.5)
    assert _parse_occ("O:SPY1261120P00765000") is None
