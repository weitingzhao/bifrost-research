"""Settle option suggestions with the simulator's walk (design §4, §5).

For each option suggestion still missing a basis at the current method version:

- ``model``: its own legs, entered at the session after ``as_of_session`` at
  vwap with tiered slippage, managed by its own rules to an exit;
- ``model_stress``: the same walk at 1.5x slippage (threshold 4);
- ``baseline_paired``: SPY, same session, same structure, the same short delta
  and days to expiry, the same rules — the pair threshold 5 compares against.
  The baseline source itself has no pair.

A position still open when the data ends writes nothing and is walked again on
the next run. One that could not have been opened writes ``void`` with the
reason, once the data has moved past its entry session.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from bifrost_research.engines.backtest.sim.chain import ChainStore
from bifrost_research.engines.backtest.sim.engine import _open
from bifrost_research.engines.backtest.sim.rules import SimConfig
from bifrost_research.engines.backtest.sim.structures import STRUCTURES
from bifrost_research.engines.backtest.sim.walk import (
    LegSpec,
    WalkOutcome,
    WalkRules,
    legs_from_json,
    load_leg_store,
    walk_legs,
)
from bifrost_research.engines.suggestion import store
from bifrost_research.engines.suggestion.config import (
    BASELINE_SYMBOL,
    BASIS_SLIPPAGE,
    MAX_STALE_SESSIONS,
    SETTLEMENT_METHOD_VERSION,
)
from bifrost_research.repositories.listing_lineage import listing_end

logger = logging.getLogger(__name__)

OWN_BASES = ("model", "model_stress")
ALL_BASES = ("model", "model_stress", "baseline_paired")


def _num(v: Any) -> float | None:
    return None if v is None else float(v)


def rules_for(s: dict[str, Any], basis: str) -> WalkRules:
    return WalkRules(
        profit_take_pct=_num(s.get("take_profit_pct")),
        stop_loss_mult=_num(s.get("stop_loss_mult")),
        dte_exit=None if s.get("exit_dte") is None else int(s["exit_dte"]),
        max_hold_days=None if s.get("max_hold_days") is None else int(s["max_hold_days"]),
        max_stale_sessions=MAX_STALE_SESSIONS,
        slippage_scale=BASIS_SLIPPAGE[basis],
    )


def entry_session(sessions: list[date], as_of: date) -> date | None:
    return next((d for d in sessions if d > as_of), None)


def settlement_row(
    suggestion_id: str,
    basis: str,
    out: WalkOutcome,
    *,
    entry: date | None,
    rules: WalkRules,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "suggestion_id": suggestion_id,
        "basis": basis,
        "method_version": SETTLEMENT_METHOD_VERSION,
        "status": out.status,
        "void_reason": out.reason if out.status == "void" else None,
        "entry_date": entry,
        "detail_json": {"rules": rules.__dict__, **(extra or {})},
    }
    if out.status != "settled":
        return row
    t = out.trade
    max_loss = _num(t.get("max_loss"))
    margin = _num(t.get("margin"))
    risk = max_loss if max_loss is not None and max_loss > 0 else margin
    net = float(t["pnl"])
    row.update(
        {
            "exit_date": date.fromisoformat(t["exit_date"]),
            "exit_reason": t["exit_reason"],
            "days_held": int(t["days_held"]),
            "entry_credit": _num(t.get("entry_credit")),
            "gross_pnl": round(out.gross_pnl or 0.0, 2),
            "slippage_cost": round(out.entry_slippage + out.exit_slippage, 2),
            "commission": out.commission,
            "net_pnl": net,
            "max_loss": max_loss,
            "margin": margin,
            "risk_basis": risk,
            "return_on_risk": round(net / risk, 6) if risk else None,
            "mfe": _num(t.get("mfe")),
            "mae": _num(t.get("mae")),
            "fill_basis": t.get("fill_basis"),
        }
    )
    row["detail_json"]["legs"] = t.get("legs")
    return row


def _walk(st: ChainStore, legs: list[LegSpec], s: dict[str, Any], basis: str) -> tuple[WalkOutcome, date | None, WalkRules]:
    rules = rules_for(s, basis)
    entry = entry_session(st.sessions, s["as_of_session"])
    if entry is None:
        return WalkOutcome("open", "no_entry_session_yet"), None, rules
    out = walk_legs(st, legs, entry, rules, structure=s.get("structure") or "custom")
    if out.status == "void" and st.sessions[-1] <= entry:
        # The entry session's prints may still be landing; void only once past it.
        return WalkOutcome("open", "entry_session_is_latest"), entry, rules
    return out, entry, rules


def _paired_legs(s: dict[str, Any], spy: ChainStore) -> list[LegSpec] | str:
    structure = s.get("structure")
    if structure not in STRUCTURES:
        return "no_paired_structure"
    snap = s.get("snapshot_json") or {}
    deltas = [abs(float(x["delta"])) for x in snap.get("legs") or [] if x.get("delta") is not None]
    if not deltas:
        return "no_delta_in_snapshot"
    sel = snap.get("selection") or {}
    as_of = s["as_of_session"]
    expiries = [date.fromisoformat(str(lg["expiry"])) for lg in s["legs_json"]]
    dte = (min(expiries) - as_of).days
    cfg = SimConfig(
        structure=structure,
        short_delta=deltas[0],
        target_dte=dte,
        min_dte=1,
        wing_width_pct=float(sel.get("wing_width_pct", 0.05)),
    )
    pos = _open(spy, as_of, cfg)
    if isinstance(pos, str):
        return f"paired_{pos}"
    return [
        LegSpec(lg.ticker, lg.right, lg.side, lg.strike, lg.expiry, lg.qty, lg.label)  # type: ignore[arg-type]
        for lg in pos.legs
    ]


def run_settle(conn: Any, *, today: date | None = None) -> dict[str, Any]:
    now = today or date.today()
    pending = store.pending_settlements(conn, ALL_BASES, SETTLEMENT_METHOD_VERSION)
    written: dict[str, int] = {}
    still_open = 0
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for s in pending:
        by_symbol.setdefault(str(s["symbol"]), []).append(s)

    for sym, rows in by_symbol.items():
        parsed: dict[str, list[LegSpec]] = {}
        for s in rows:
            try:
                parsed[s["suggestion_id"]] = legs_from_json(s["legs_json"])
            except (KeyError, ValueError) as exc:
                logger.warning("suggestion %s legs unreadable: %s", s["suggestion_id"], exc)
        tickers = {lg.ticker for legs in parsed.values() for lg in legs}
        start = min(s["as_of_session"] for s in rows)
        end = max((lg.expiry for legs in parsed.values() for lg in legs), default=start) + timedelta(days=3)
        st = load_leg_store(conn, sym, tickers, start, min(end, now))
        st.delisted_on = listing_end(conn, sym, as_of=now)
        for s in rows:
            legs = parsed.get(s["suggestion_id"])
            for basis in OWN_BASES:
                if basis in s["done"]:
                    continue
                if legs is None:
                    out, entry, rules = WalkOutcome("void", "legs_unreadable"), None, rules_for(s, basis)
                else:
                    out, entry, rules = _walk(st, legs, s, basis)
                if out.status == "open":
                    still_open += 1
                    continue
                if store.insert_settlement(conn, settlement_row(s["suggestion_id"], basis, out, entry=entry, rules=rules)):
                    written[basis] = written.get(basis, 0) + 1

    spy_chains: dict[tuple[date, int], ChainStore] = {}
    for s in pending:
        if s["source"] == "baseline" or "baseline_paired" in s["done"]:
            continue
        as_of = s["as_of_session"]
        expiries = [date.fromisoformat(str(lg["expiry"])) for lg in s["legs_json"]]
        dte = (min(expiries) - as_of).days
        key = (as_of, dte)
        if key not in spy_chains:
            spy_chains[key] = ChainStore.load(conn, BASELINE_SYMBOL, as_of, as_of, max_dte=dte)
        spy = spy_chains[key]
        legs_or_reason = _paired_legs(s, spy)
        basis = "baseline_paired"
        if isinstance(legs_or_reason, str):
            if as_of not in spy.spot:
                still_open += 1
                continue
            out, entry, rules = WalkOutcome("void", legs_or_reason), None, rules_for(s, basis)
        else:
            out, entry, rules = _walk(spy, legs_or_reason, s, basis)
        if out.status == "open":
            still_open += 1
            continue
        extra = {"paired_symbol": BASELINE_SYMBOL}
        if store.insert_settlement(conn, settlement_row(s["suggestion_id"], basis, out, entry=entry, rules=rules, extra=extra)):
            written[basis] = written.get(basis, 0) + 1
    conn.commit()
    return {
        "method_version": SETTLEMENT_METHOD_VERSION,
        "pending_suggestions": len(pending),
        "written": written,
        "still_open": still_open,
    }


__all__ = ["ALL_BASES", "entry_session", "rules_for", "run_settle", "settlement_row"]
