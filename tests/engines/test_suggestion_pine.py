"""Pine signals as a suggestion source (S4): mapping and issuance rules."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from bifrost_research.engines.backtest.canonical_pnl import bs_price
from bifrost_research.engines.backtest.sim.chain import ChainStore, OptBar
from bifrost_research.engines.suggestion import pine
from bifrost_research.engines.suggestion.config import DELTA_TOLERANCE, PINE_LIVE, SIMULATOR_LIVE
from bifrost_research.engines.suggestion.contract import Suggestion
from bifrost_research.engines.suggestion.issue import build_suggestion
from bifrost_research.engines.suggestion.settle import _paired_legs

LIVE = PINE_LIVE["live_from"]
MON = date(2026, 10, 12)  # a Monday after live_from


def _cfg(**kw: Any) -> dict[str, Any]:
    return {**PINE_LIVE, **kw}


def _sig(sym: str, d: date, side: str = "buy", script: str = "supertrend", version: int = 1) -> dict[str, Any]:
    return {"script_id": script, "symbol": sym, "trade_date": d, "side": side, "script_version": version, "close": 100.0}


def _row(sym: str, d: date, side: str = "buy", script: str = "supertrend") -> dict[str, Any]:
    return {"script_id": script, "symbol": sym, "as_of": d, "structure": PINE_LIVE["sides"][side]["structure"]}


# Other names' suggestions from long ago, so the share cap is not what a test meets.
FILLER = [{"script_id": sc, "symbol": f"Z{i}", "as_of": MON - timedelta(days=90), "structure": "short_put"} for sc in ("supertrend", "donchian_breakout") for i in range(40)]


def _fake(sig: dict[str, Any]) -> Suggestion:
    side = PINE_LIVE["sides"][sig["side"]]
    return Suggestion(
        as_of_session=sig["trade_date"],
        source="pine",
        source_ref=sig["script_id"],
        source_version=str(sig["script_version"]),
        symbol=sig["symbol"],
        kind="option_structure",
        structure=side["structure"],
        legs=(
            {
                "contract_key": "O:X",
                "right": "P",
                "side": "sell",
                "strike": 90.0,
                "expiry": (sig["trade_date"] + timedelta(days=45)).isoformat(),
                "ratio": 1,
            },
        ),
        take_profit_pct=0.5,
    )


class _Ledger:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.built: list[tuple[str, date, str]] = []
        self.written: dict[str, Suggestion] = {}
        self.fail = fail or set()

    def build(self, sig: dict[str, Any]) -> Suggestion | str:
        self.built.append((sig["symbol"], sig["trade_date"], sig["side"]))
        if sig["symbol"] in self.fail:
            return "delta_out_of_band"
        return _fake(sig)

    def write(self, s: Suggestion) -> bool:
        if s.issue_key in {x.issue_key for x in self.written.values()}:
            return False
        self.written[s.suggestion_id] = s
        return True

    def rows(self) -> list[dict[str, Any]]:
        return [
            {"script_id": s.source_ref, "symbol": s.symbol, "as_of": s.as_of_session, "structure": s.structure}
            for s in self.written.values()
        ]


def _run(signals: list[dict[str, Any]], issued: list[dict[str, Any]] | None = None, led: _Ledger | None = None, **cfg: Any):
    led = led or _Ledger()
    out = pine.issue_pine(signals, issued or [], build=led.build, write=led.write, cfg=_cfg(**cfg))
    return out, led


# -- mapping ----------------------------------------------------------------------


def test_buy_sells_the_20_delta_put_sell_sells_the_20_delta_call_credit_spread() -> None:
    buy = pine.spec_for("supertrend", 1, "buy")
    sell = pine.spec_for("donchian_breakout", 3, "sell")
    assert (buy["structure"], buy["short_delta"]) == ("short_put", 0.20)
    assert (sell["structure"], sell["short_delta"]) == ("call_credit_spread", 0.20)
    assert sell["source_ref"] == "donchian_breakout" and sell["source_version"] == "3"
    sim = SIMULATOR_LIVE[0]
    for spec in (buy, sell):
        for k in ("target_dte", "min_dte", "wing_width_pct", "take_profit_pct", "stop_loss_mult", "exit_dte"):
            assert spec[k] == sim[k], k
    assert pine.side_of("short_put") == "buy" and pine.side_of("call_credit_spread") == "sell"
    assert PINE_LIVE["cooldown_days"] == sim["target_dte"] - sim["exit_dte"]
    assert [s["script_id"] for s in PINE_LIVE["scripts"]] == ["supertrend", "donchian_breakout"]


def _store(spot: float = 100.0, n: int = 30, symbol: str = "X") -> ChainStore:
    days, d = [], MON
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    fridays = [x for x in (MON + timedelta(days=i) for i in range(120)) if x.weekday() == 4]
    bars: list[OptBar] = []
    for d in days:
        for exp in fridays:
            dte = (exp - d).days
            if dte < 1 or dte > 90:
                continue
            for k2 in range(120, 290, 5):
                k = k2 / 2 * spot / 100.0
                for right in ("C", "P"):
                    px = bs_price(spot, k, dte / 365.0, 0.30, right=right)
                    if px >= 0.01:
                        bars.append(OptBar(f"O:{symbol}{exp:%y%m%d}{right}{int(k * 1000):08d}", exp, k, right, d, round(px, 4), None, 10))
    return ChainStore(symbol, {d: spot for d in days}, bars)


def test_a_sell_signal_builds_a_complete_call_credit_spread_at_20_delta() -> None:
    st = _store()
    got = build_suggestion(st, MON, source="pine", spec=pine.spec_for("donchian_breakout", 1, "sell"), regime={})
    assert isinstance(got, Suggestion)
    got.validate()
    short, long_ = got.legs
    assert (short["right"], short["side"], long_["right"], long_["side"]) == ("C", "sell", "C", "buy")
    assert long_["strike"] > short["strike"] > 100
    delta = got.snapshot["legs"][0]["delta"]
    assert abs(delta - 0.20) <= DELTA_TOLERANCE
    assert got.max_loss == pytest.approx((long_["strike"] - short["strike"]) * 100 - got.expected_credit, abs=0.02)
    assert got.issue_key == f"pine:donchian_breakout@1:{MON.isoformat()}:X"


def test_a_buy_signal_builds_a_20_delta_short_put() -> None:
    got = build_suggestion(_store(), MON, source="pine", spec=pine.spec_for("supertrend", 1, "buy"), regime={})
    assert isinstance(got, Suggestion)
    (leg,) = got.legs
    assert (leg["right"], leg["side"]) == ("P", "sell") and leg["strike"] < 100
    assert abs(abs(got.snapshot["legs"][0]["delta"]) - 0.20) <= DELTA_TOLERANCE


def test_the_paired_spy_baseline_mirrors_a_call_credit_spread() -> None:
    st = _store()
    got = build_suggestion(st, MON, source="pine", spec=pine.spec_for("donchian_breakout", 1, "sell"), regime={})
    assert isinstance(got, Suggestion)
    row = {
        "structure": got.structure,
        "snapshot_json": got.snapshot,
        "as_of_session": MON,
        "legs_json": list(got.legs),
    }
    spy = _store(spot=700.0, symbol="SPY")
    legs = _paired_legs(row, spy)
    assert not isinstance(legs, str)
    assert [(lg.right, lg.side) for lg in legs] == [("C", "sell"), ("C", "buy")]
    assert legs[1].strike > legs[0].strike > 700


# -- issuance rules ---------------------------------------------------------------


def test_one_suggestion_per_signal_and_a_rerun_writes_nothing() -> None:
    sigs = [_sig("AAA", MON), _sig("BBB", MON, "sell")]
    out, led = _run(sigs)
    assert out["written"] == 2
    again, led2 = _run(sigs, issued=led.rows())
    assert again["written"] == 0
    assert again["skipped"] == {"already_issued": 2}
    assert led2.built == []  # already-issued signals are not even built


def test_signals_before_live_from_and_unpinned_versions_are_not_issued() -> None:
    out, led = _run([_sig("AAA", LIVE - timedelta(days=1)), _sig("BBB", MON, version=2), _sig("CCC", MON, script="wavetrend")])
    assert out["written"] == 0
    assert out["signals"] == 1  # only BBB is a pinned script on or after live_from
    assert out["skipped"] == {"script_version_not_pinned": 1}


def test_both_sides_on_the_same_session_issue_neither() -> None:
    out, _ = _run([_sig("AAA", MON, "buy"), _sig("AAA", MON, "sell")])
    assert out["written"] == 0 and out["skipped"] == {"both_sides_same_session": 2}


def test_one_per_symbol_per_iso_week_across_sides() -> None:
    out, led = _run([_sig("AAA", MON), _sig("AAA", MON + timedelta(days=2), "sell")])
    assert out["written"] == 1 and out["skipped"] == {"iso_week_cap": 1}
    # Next week the opposite side is new information: no cooldown across sides.
    nxt, _ = _run([_sig("AAA", MON + timedelta(days=7), "sell")], issued=led.rows() + FILLER)
    assert nxt["written"] == 1


def test_a_same_direction_repeat_waits_out_the_cooldown() -> None:
    issued = [_row("AAA", MON)] + FILLER
    blocked, _ = _run([_sig("AAA", MON + timedelta(days=PINE_LIVE["cooldown_days"] - 3))], issued=issued)
    assert blocked["skipped"] == {"cooldown": 1}
    ok, _ = _run([_sig("AAA", MON + timedelta(days=PINE_LIVE["cooldown_days"] + 4))], issued=issued)
    assert ok["written"] == 1
    # Each script has its own book: donchian on AAA is not held back by supertrend.
    other, _ = _run([_sig("AAA", MON + timedelta(days=7), script="donchian_breakout")], issued=issued)
    assert other["written"] == 1


def test_no_symbol_takes_more_than_its_share_of_a_scripts_suggestions() -> None:
    old = MON - timedelta(days=60)
    few = [_row("AAA", old)] + [_row(f"S{i}", old) for i in range(4)]
    blocked, _ = _run([_sig("AAA", MON)], issued=few)
    assert blocked["skipped"] == {"symbol_share_cap": 1}  # 2 of 6 > 10%
    many = [_row("AAA", old)] + [_row(f"S{i}", old) for i in range(18)]
    ok, _ = _run([_sig("AAA", MON)], issued=many)
    assert ok["written"] == 1  # 2 of 20 = 10%
    first, _ = _run([_sig("NEW", MON)], issued=few)
    assert first["written"] == 1  # a symbol's first suggestion is always allowed


def test_daily_cap_per_side_in_hash_order_and_failures_do_not_use_it() -> None:
    cap = PINE_LIVE["daily_cap_per_side"]
    syms = [f"S{i:02d}" for i in range(12)]
    buys = [_sig(s, MON) for s in syms]
    sells = [_sig(s, MON, "sell", script="donchian_breakout") for s in syms]
    out, led = _run(buys + sells, issued=[_row(f"Z{i}", MON - timedelta(days=90)) for i in range(40)])
    assert out["written"] == 2 * cap
    assert out["skipped"]["daily_cap"] == 2 * (len(syms) - cap)
    expect = sorted(syms, key=lambda s: pine.rank(MON, "supertrend", "buy", s))[:cap]
    assert sorted(s.symbol for s in led.written.values() if s.source_ref == "supertrend") == sorted(expect)
    # Same input in another order picks the same names.
    out2, led2 = _run(list(reversed(buys)), issued=[_row(f"Z{i}", MON - timedelta(days=90)) for i in range(40)])
    assert {s.symbol for s in led2.written.values()} == set(expect)
    # A name the chain cannot express is skipped and the next one takes its place.
    first = expect[0]
    out3, led3 = _run(buys, issued=[_row(f"Z{i}", MON - timedelta(days=90)) for i in range(40)], led=_Ledger(fail={first}))
    assert out3["written"] == cap and out3["skipped"]["delta_out_of_band"] == 1
    assert first not in {s.symbol for s in led3.written.values()}


def test_a_later_run_counts_what_the_ledger_holds_toward_the_daily_cap() -> None:
    cap = PINE_LIVE["daily_cap_per_side"]
    issued = [_row(f"S{i}", MON) for i in range(cap)] + [_row(f"Z{i}", MON - timedelta(days=90)) for i in range(40)]
    out, _ = _run([_sig("NEW", MON)], issued=issued)
    assert out["skipped"] == {"daily_cap": 1}


def test_the_pine_issue_failing_does_not_stop_settlement(monkeypatch: pytest.MonkeyPatch) -> None:
    from bifrost_research.engines.suggestion import entry

    class _Conn:
        rolled = 0

        def rollback(self) -> None:
            _Conn.rolled += 1

        def close(self) -> None:
            pass

    monkeypatch.setattr(entry, "connect", lambda: _Conn())
    monkeypatch.setattr(entry, "run_issue", lambda conn, today=None: {"written": 1})

    def _boom(conn: Any, today: Any = None) -> dict[str, Any]:
        raise RuntimeError("runner down")

    monkeypatch.setattr(entry, "run_issue_pine", _boom)
    monkeypatch.setattr(entry, "run_settle", lambda conn, today=None: {"written": {"model": 2}})
    out = entry.run(today=MON)
    assert out["pine"] == {"error": "RuntimeError: runner down"}
    assert out["settled"] == {"written": {"model": 2}}
    assert _Conn.rolled == 1
