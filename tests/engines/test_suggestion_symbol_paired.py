"""The symbol_paired basis (S4 control, Owner 2026-10-06 option A)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from bifrost_research.engines.backtest.canonical_pnl import bs_price
from bifrost_research.engines.backtest.sim.chain import ChainStore, OptBar
from bifrost_research.engines.suggestion import pine, settle, store
from bifrost_research.engines.suggestion.contract import Suggestion
from bifrost_research.engines.suggestion.issue import build_suggestion
from bifrost_research.schema import migrate_symbol_paired as mig
from bifrost_research.schema.suggestion_ledger_ddl import SETTLEMENT_BASES, ledger_statements

MON = date(2026, 10, 12)


def _days(n: int, start: date = MON - timedelta(days=21)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _store(symbol: str = "X", spot: float = 100.0) -> ChainStore:
    days = _days(90)
    fridays = [x for x in (days[0] + timedelta(days=i) for i in range(200)) if x.weekday() == 4]
    bars: list[OptBar] = []
    for d in days:
        for exp in fridays:
            dte = (exp - d).days
            if dte < 0 or dte > 90:
                continue
            for k2 in range(120, 290, 5):
                k = k2 / 2
                for right in ("C", "P"):
                    px = bs_price(spot, k, max(dte, 0.5) / 365.0, 0.30, right=right)
                    if px >= 0.01:
                        bars.append(OptBar(f"O:{symbol}{exp:%y%m%d}{right}{int(k * 1000):08d}", exp, k, right, d, round(px, 4), None, 10))
    return ChainStore(symbol, {d: spot for d in days}, bars)


def _row(st: ChainStore, side: str = "sell") -> dict[str, Any]:
    got = build_suggestion(st, MON, source="pine", spec=pine.spec_for("donchian_breakout", 1, side), regime={})
    assert isinstance(got, Suggestion)
    return {
        "suggestion_id": got.suggestion_id,
        "as_of_session": MON,
        "source": "pine",
        "source_ref": "donchian_breakout",
        "source_version": "1",
        "symbol": "X",
        "structure": got.structure,
        "legs_json": list(got.legs),
        "take_profit_pct": got.take_profit_pct,
        "stop_loss_mult": got.stop_loss_mult,
        "exit_dte": got.exit_dte,
        "max_hold_days": None,
        "snapshot_json": got.snapshot,
        "done": set(),
    }


# -- schema -----------------------------------------------------------------------


def test_the_basis_is_listed_and_the_recurring_ddl_stays_drop_free() -> None:
    assert "symbol_paired" in SETTLEMENT_BASES
    assert "'symbol_paired'" in "\n".join(ledger_statements())
    assert "DROP" not in "\n".join(ledger_statements()).upper()


def test_the_one_off_migration_swaps_the_check_in_one_transaction() -> None:
    sql = mig.statements()
    assert sql[0].startswith("SET LOCAL lock_timeout")
    assert sql[1] == "ALTER TABLE research.suggestion_settlement DROP CONSTRAINT suggestion_settlement_basis_check"
    assert sql[2].endswith(
        "CHECK (basis IN ('model', 'model_stress', 'baseline_paired', 'actual', 'symbol_paired'))"
    )


class _Cur:
    def __init__(self, definition: str | None) -> None:
        self.definition = definition
        self.ran: list[str] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None:
        pass

    def execute(self, sql: str, params: Any = None) -> None:
        self.ran.append(sql)
        if sql.startswith("ALTER TABLE") and "ADD CONSTRAINT" in sql:
            self.definition = "CHECK ((basis = ANY (ARRAY['model'::text, 'symbol_paired'::text])))"

    def fetchone(self) -> Any:
        return (self.definition,) if self.definition else None


class _Conn:
    def __init__(self, cur: _Cur) -> None:
        self.c = cur
        self.committed = self.rolled = 0

    def cursor(self) -> _Cur:
        return self.c

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled += 1


def test_the_migration_runs_once() -> None:
    old = _Conn(_Cur("CHECK ((basis = ANY (ARRAY['model'::text, 'actual'::text])))"))
    out = mig.apply(old)
    assert out["applied"] is True and "symbol_paired" in out["after"] and old.committed == 1
    again = _Conn(_Cur(out["after"]))
    assert mig.apply(again) == {"applied": False, "reason": "already widened", "definition": out["after"]}
    assert not any(s.startswith("ALTER") for s in again.c.ran)


class _PendingCur(_Cur):
    def __init__(self) -> None:
        super().__init__(None)
        self.params: Any = None
        self.description = [("x",)]

    def execute(self, sql: str, params: Any = None) -> None:
        self.params = params

    def fetchall(self) -> list[Any]:
        return []


def test_pending_counts_the_extra_basis_only_for_signal_sources() -> None:
    cur = _PendingCur()
    store.pending_settlements(_Conn(cur), settle.ALL_BASES, "walk-1", symbol_paired_sources=("pine",))
    _mv, _mv2, counted, unpaired, n_own, sources, n_paired, n_all = cur.params
    assert counted == ["model", "model_stress", "baseline_paired", "symbol_paired"]
    assert (unpaired, n_own, sources, n_paired, n_all) == ("baseline", 2, ["pine"], 4, 3)


# -- the control ------------------------------------------------------------------


def test_the_control_day_is_after_the_signal_and_not_a_fired_session() -> None:
    days = _days(30)
    d = MON
    fired = {d + timedelta(days=1)}
    c = settle.control_day(days, d, fired, "sg_1", 7)
    assert c is not None and d < c <= d + timedelta(days=7) and c not in fired
    assert c == settle.control_day(days, d, fired, "sg_1", 7)
    # Never before the signal: those sessions carry the move that made it.
    before = [x for x in days if x < d]
    assert settle.control_day([*before, d], d, set(), "sg_1", 7) is None


def test_the_control_rebuilds_the_rule_not_the_strikes() -> None:
    spec = settle.control_spec(_row(_store()))
    assert (spec["structure"], spec["short_delta"], spec["target_dte"]) == ("call_credit_spread", 0.20, 45)
    assert spec["take_profit_pct"] == 0.5 and spec["exit_dte"] == 21


def _wire(monkeypatch: pytest.MonkeyPatch, st: ChainStore, fired: set[date]) -> None:
    monkeypatch.setattr(store, "recent_sessions", lambda conn, sym, end, days=30: [d for d in st.sessions if d <= end])
    monkeypatch.setattr(store, "fired_sessions", lambda conn, script, sym, a, b: set(fired))
    monkeypatch.setattr(settle.ChainStore, "load", classmethod(lambda cls, conn, sym, a, b, max_dte: st))
    monkeypatch.setattr(settle, "with_snapshot_fill", lambda conn, chain, a, b: chain)
    monkeypatch.setattr(settle, "load_leg_store", lambda conn, sym, tickers, a, b: st)
    monkeypatch.setattr(settle, "listing_end", lambda conn, sym, as_of: None)


def test_symbol_paired_waits_for_the_window_then_settles_the_same_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    st = _store()
    row = _row(st)
    _wire(monkeypatch, st, {MON})
    out, _entry, _x = settle._symbol_paired(None, row, MON + timedelta(days=3), MON + timedelta(days=3))
    assert (out.status, out.reason) == ("open", "control_window_open")
    later = st.sessions[-1]
    out, entry, extra = settle._symbol_paired(None, row, later, later)
    assert out.status == "settled"
    c = date.fromisoformat(extra["control_day"])
    assert MON < c <= MON + timedelta(days=7) and entry is not None and entry > c
    assert [(lg["right"], lg["side"]) for lg in extra["control_legs"]] == [("C", "sell"), ("C", "buy")]
    assert abs(extra["control_delta"] - 0.20) <= 0.05
    srow = settle.settlement_row(row["suggestion_id"], settle.SYMBOL_BASIS, out, entry=entry, rules=settle.rules_for(row, "symbol_paired"), extra=extra)
    assert srow["basis"] == "symbol_paired" and srow["return_on_risk"] is not None


def test_no_quiet_session_in_the_window_is_a_void(monkeypatch: pytest.MonkeyPatch) -> None:
    st = _store()
    row = _row(st)
    _wire(monkeypatch, st, set(st.sessions))
    out, _e, _x = settle._symbol_paired(None, row, st.sessions[-1], st.sessions[-1])
    assert (out.status, out.reason) == ("void", "no_control_day")
