"""The session loop: open on a schedule, mark daily, manage, settle.

Every session, for each open position (in order): settle at intrinsic if the
session has reached expiry; otherwise mark each leg at that session's print
(carrying the last mark and counting stale sessions when it did not trade) and
check stale → profit take → stop → DTE exit. Then, on entry sessions, open a
new position if the symbol has room. Exits fill at the session's mark with the
tiered slippage; expiry settles at intrinsic with no slippage or commission.

Ordering note: a rule is checked on the session's close and filled at that
same close. Real fills come later than the signal, so read the stop and profit
figures as optimistic by up to a session's move.
"""

from __future__ import annotations

import bisect
import logging
import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Sequence

from bifrost_research.engines.backtest.catalog import evaluation
from bifrost_research.engines.backtest.sim.chain import ChainStore, OptBar
from bifrost_research.engines.backtest.sim.rules import (
    SimConfig,
    fill_basis,
    fill_price,
    naked_short_requirement,
)
from bifrost_research.engines.backtest.sim.structures import STRUCTURES, SimLeg
from bifrost_research.repositories.listing_lineage import listing_end, live_label

logger = logging.getLogger(__name__)

# Below this an OTM leg without a print is left at its last mark rather than
# counted stale.
_CHEAP_OTM = 0.10


@dataclass
class _Leg:
    label: str
    ticker: str
    right: str
    side: str
    strike: float
    expiry: date
    qty: int
    entry_mark: float
    entry_fill: float
    mark: float
    entry_iv: float | None = None
    entry_delta: float | None = None
    stale: int = 0

    @property
    def sign(self) -> int:
        return 1 if self.side == "buy" else -1


@dataclass
class _Position:
    symbol: str
    structure: str
    entry_date: date
    expiry: date
    legs: list[_Leg]
    mult: int
    commission: float
    margin: float
    max_loss: float | None
    path: list[float] = field(default_factory=list)

    def entry_value(self) -> float:
        return sum(lg.sign * lg.entry_fill * lg.qty for lg in self.legs) * self.mult

    def credit(self) -> float:
        return -self.entry_value()

    def mark_value(self) -> float:
        return sum(lg.sign * lg.mark * lg.qty for lg in self.legs) * self.mult

    def open_pnl(self) -> float:
        return self.mark_value() - self.entry_value() - self.commission


@dataclass
class SimResult:
    summary: dict[str, Any]
    trades: list[dict[str, Any]]
    equity: list[dict[str, Any]]
    params: dict[str, Any]


# -- entry ------------------------------------------------------------------------


def _pick_by_delta(
    store: ChainStore, d: date, leg: SimLeg, cfg: SimConfig, expiry: date | None
) -> tuple[OptBar, tuple[float, float]] | str:
    chain = [b for b in store.chain_on(d, leg.right) if (b.expiry - d).days >= cfg.min_dte]
    if not chain:
        return "no_chain"
    if expiry is None:
        target = d + timedelta(days=cfg.target_dte)
        expiry = min({b.expiry for b in chain}, key=lambda e: (abs((e - target).days), e))
    pool = [b for b in chain if b.expiry == expiry]
    want = abs(cfg.short_delta)
    best: tuple[float, OptBar, tuple[float, float]] | None = None
    for b in pool:
        g = store.iv_delta(b, d, cfg.price_field, use_rate=cfg.use_treasury_rate)
        if g is None:
            continue
        score = abs(abs(g[1]) - want)
        if best is None or (score, b.strike) < (best[0], best[1].strike):
            best = (score, b, g)
    if best is None:
        return "no_iv"
    return best[1], best[2]


def _pick_anchored(store: ChainStore, d: date, leg: SimLeg, anchor: _Leg, cfg: SimConfig) -> OptBar | str:
    spot = store.spot[d]
    target = anchor.strike + leg.offset_sign * cfg.wing_width_pct * spot
    pool = [
        b
        for b in store.chain_on(d, leg.right)
        if b.expiry == anchor.expiry and (b.strike - anchor.strike) * leg.offset_sign > 0
    ]
    if not pool:
        return "no_wing"
    return min(pool, key=lambda b: (abs(b.strike - target), b.strike))


def _margin(structure: str, legs: list[_Leg], spot: float, credit: float, mult: int) -> tuple[float, float | None]:
    """(margin requirement, max loss or None when undefined) for the whole position."""
    qty = max(lg.qty for lg in legs)
    shorts = [lg for lg in legs if lg.side == "sell"]
    longs = [lg for lg in legs if lg.side == "buy"]
    if longs:
        widths = []
        for s in shorts:
            wing = next((lg for lg in longs if lg.right == s.right), None)
            if wing is not None:
                widths.append(abs(wing.strike - s.strike))
        width = max(widths) if widths else 0.0
        max_loss = max(0.0, width * mult * qty - credit)
        return max_loss, max_loss
    reqs = [naked_short_requirement(spot, s.strike, s.entry_mark, s.right) for s in shorts]
    if len(shorts) == 1:
        return reqs[0] * mult * qty, None
    # A strangle: the larger side's requirement plus the other side's premium.
    best = 0.0
    for i, s in enumerate(shorts):
        other = sum(o.entry_mark for j, o in enumerate(shorts) if j != i)
        best = max(best, reqs[i] + other)
    return best * mult * qty, None


def _open(store: ChainStore, d: date, cfg: SimConfig) -> _Position | str:
    specs = STRUCTURES[cfg.structure]
    legs: list[_Leg] = []
    expiry: date | None = None
    for spec in specs:
        if spec.anchor is None:
            got = _pick_by_delta(store, d, spec, cfg, expiry)
            if isinstance(got, str):
                return got
            bar, (iv, delta) = got
            expiry = bar.expiry
        else:
            got2 = _pick_anchored(store, d, spec, legs[spec.anchor], cfg)
            if isinstance(got2, str):
                return got2
            bar, iv, delta = got2, None, None
        mark = bar.price(cfg.price_field)
        legs.append(
            _Leg(
                label=spec.label,
                ticker=bar.ticker,
                right=bar.right,
                side=spec.side,
                strike=bar.strike,
                expiry=bar.expiry,
                qty=cfg.quantity,
                entry_mark=mark,
                entry_fill=fill_price(mark, spec.side, cfg.slippage_scale),
                mark=mark,
                entry_iv=iv,
                entry_delta=delta,
            )
        )
    assert expiry is not None
    commission = cfg.commission_per_contract * sum(lg.qty for lg in legs)
    pos = _Position(
        symbol=store.symbol,
        structure=cfg.structure,
        entry_date=d,
        expiry=expiry,
        legs=legs,
        mult=cfg.multiplier,
        commission=commission,
        margin=0.0,
        max_loss=None,
    )
    if pos.credit() <= 0:
        return "no_credit"
    pos.margin, pos.max_loss = _margin(cfg.structure, legs, store.spot[d], pos.credit(), cfg.multiplier)
    pos.path.append(pos.open_pnl())
    return pos


# -- exit -------------------------------------------------------------------------


def _close(pos: _Position, d: date, reason: str, cfg: SimConfig, *, settle_spot: float | None = None) -> dict[str, Any]:
    legs_out: list[dict[str, Any]] = []
    exit_value = 0.0
    commission = pos.commission
    for lg in pos.legs:
        if settle_spot is not None:
            px = max(0.0, settle_spot - lg.strike) if lg.right == "C" else max(0.0, lg.strike - settle_spot)
        else:
            px = fill_price(lg.mark, "sell" if lg.side == "buy" else "buy", cfg.slippage_scale)
            commission += cfg.commission_per_contract * lg.qty
        exit_value += lg.sign * px * lg.qty * pos.mult
        legs_out.append(
            {
                "label": lg.label,
                "ticker": lg.ticker,
                "right": lg.right,
                "side": lg.side,
                "strike": lg.strike,
                "expiry": lg.expiry.isoformat(),
                "qty": lg.qty,
                "entry_mark": round(lg.entry_mark, 4),
                "entry_fill": round(lg.entry_fill, 4),
                "exit_fill": round(px, 4),
                "entry_iv": round(lg.entry_iv, 4) if lg.entry_iv is not None else None,
                "entry_delta": round(lg.entry_delta, 4) if lg.entry_delta is not None else None,
                "stale_sessions": lg.stale,
            }
        )
    pnl = exit_value - pos.entry_value() - commission
    if reason == "expiry" and settle_spot is not None:
        itm = any(
            (lg.side == "sell")
            and ((lg.right == "C" and settle_spot > lg.strike) or (lg.right == "P" and settle_spot < lg.strike))
            for lg in pos.legs
        )
        if itm:
            # Assignment is cash-settled at intrinsic here; the shares are not carried.
            reason = "expiry_itm"
    path = pos.path + [pnl]
    return {
        "symbol": pos.symbol,
        "structure": pos.structure,
        "entry_date": pos.entry_date.isoformat(),
        "exit_date": d.isoformat(),
        "exit_reason": reason,
        "legs": legs_out,
        "entry_credit": round(pos.credit(), 2),
        "exit_debit": round(-exit_value, 2),
        "pnl": round(pnl, 2),
        "max_loss": round(pos.max_loss, 2) if pos.max_loss is not None else None,
        "margin": round(pos.margin, 2),
        "days_held": (d - pos.entry_date).days,
        "mfe": round(max(path), 2),
        "mae": round(min(path), 2),
        "fill_basis": fill_basis(cfg),
    }


def _manage(pos: _Position, store: ChainStore, d: date, cfg: SimConfig) -> dict[str, Any] | None:
    if d >= pos.expiry:
        spot = store.spot_on_or_before(pos.expiry)
        if spot is None:
            return None
        return _close(pos, pos.expiry, "expiry", cfg, settle_spot=spot)
    spot = store.spot.get(d)
    for lg in pos.legs:
        b = store.bar(lg.ticker, d)
        if b is not None:
            lg.mark = b.price(cfg.price_field)
            lg.stale = 0
            continue
        otm = spot is not None and ((lg.right == "C" and spot < lg.strike) or (lg.right == "P" and spot > lg.strike))
        if otm and lg.mark < _CHEAP_OTM:
            # A cheap out-of-the-money contract that stops trading is the
            # expected end of a winning short, not a mark gone bad.
            continue
        lg.stale += 1
    pnl = pos.open_pnl()
    pos.path.append(pnl)
    credit = pos.credit()
    if cfg.max_stale_sessions is not None and any(lg.stale >= cfg.max_stale_sessions for lg in pos.legs):
        return _close(pos, d, "stale", cfg)
    if cfg.profit_take_pct is not None and pnl >= cfg.profit_take_pct * credit:
        return _close(pos, d, "profit_take", cfg)
    if cfg.stop_loss_mult is not None and pnl <= -cfg.stop_loss_mult * credit:
        return _close(pos, d, "stop", cfg)
    if cfg.dte_exit is not None and (pos.expiry - d).days <= cfg.dte_exit:
        return _close(pos, d, "dte_exit", cfg)
    return None


# -- one symbol -------------------------------------------------------------------


def _event_entries(sessions: Sequence[date], events: Sequence[date], offset: int) -> set[date]:
    """The session ``offset`` from each event; offset 0 is the first session on or after it."""
    out: set[date] = set()
    for ev in events:
        i = bisect.bisect_left(sessions, ev)
        j = i + int(offset)
        if i < len(sessions) and 0 <= j < len(sessions):
            out.add(sessions[j])
    return out


def _run_symbol(
    store: ChainStore,
    start: date,
    end: date,
    cfg: SimConfig,
    *,
    events: Sequence[date] | None = None,
) -> tuple[list[dict[str, Any]], dict[date, tuple[float, float, int]], dict[str, int]]:
    trades: list[dict[str, Any]] = []
    curve: dict[date, tuple[float, float, int]] = {}
    skips: dict[str, int] = {}
    open_: list[_Position] = []
    realized = 0.0
    entry_sessions = [d for d in store.sessions if start <= d <= end]
    if events is None:
        entry_set = {d for i, d in enumerate(entry_sessions) if i % max(1, cfg.entry_every_sessions) == 0}
    else:
        entry_set = {d for d in _event_entries(store.sessions, events, cfg.entry_offset_sessions) if start <= d <= end}
    for d in store.sessions:
        if d < start:
            continue
        if d > end and not open_:
            break
        still: list[_Position] = []
        for pos in open_:
            done = _manage(pos, store, d, cfg)
            if done is None:
                still.append(pos)
            else:
                trades.append(done)
                realized += done["pnl"]
        open_ = still
        if d in entry_set and len(open_) < cfg.max_open_per_symbol:
            got = _open(store, d, cfg)
            if isinstance(got, str):
                skips[got] = skips.get(got, 0) + 1
            else:
                open_.append(got)
        curve[d] = (
            realized + sum(p.open_pnl() for p in open_),
            sum(p.margin for p in open_),
            len(open_),
        )
    last = store.sessions[-1] if store.sessions else end
    # A delisted name's open positions close at its last marks (B7): leaving them
    # out would drop exactly the trades the listing's end went against.
    reason = "delisted" if store.delisted_on is not None and last == store.delisted_on else "end_of_data"
    for pos in open_:
        done = _close(pos, last, reason, cfg)
        trades.append(done)
        realized += done["pnl"]
    if open_ and last in curve:
        curve[last] = (realized, 0.0, 0)
    return trades, curve, skips


# -- summary ----------------------------------------------------------------------


def _bootstrap_ci(values: Sequence[float], seed: int, n: int = 1000) -> tuple[float, float] | None:
    if len(values) < 5:
        return None
    rng = random.Random(seed)
    k = len(values)
    means = sorted(statistics.fmean(rng.choices(values, k=k)) for _ in range(n))
    return round(means[int(0.025 * n)], 2), round(means[int(0.975 * n) - 1], 2)


def summarize(trades: list[dict[str, Any]], equity: list[dict[str, Any]], cfg: SimConfig) -> dict[str, Any]:
    pnls = [float(t["pnl"]) for t in trades]
    n = len(pnls)
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    eq = [float(e["equity"]) for e in equity]
    max_dd = 0.0
    peak = eq[0] if eq else cfg.capital
    for v in eq:
        peak = max(peak, v)
        max_dd = min(max_dd, v - peak)
    daily = [(eq[i] - eq[i - 1]) / cfg.capital for i in range(1, len(eq))]
    sd = statistics.pstdev(daily) if len(daily) > 1 else 0.0
    sharpe = statistics.fmean(daily) / sd * math.sqrt(252) if sd > 0 else 0.0
    peak_margin = max((float(e["margin_used"]) for e in equity), default=0.0)
    total = sum(pnls)
    return {
        "n_trades": n,
        "win_rate": round(sum(1 for p in pnls if p > 0) / n, 4) if n else 0.0,
        "total_pnl": round(total, 2),
        "avg_pnl": round(statistics.fmean(pnls), 2) if n else 0.0,
        "median_pnl": round(statistics.median(pnls), 2) if n else 0.0,
        "avg_pnl_ci95": _bootstrap_ci(pnls, cfg.bootstrap_seed),
        "avg_credit": round(statistics.fmean(float(t["entry_credit"]) for t in trades), 2) if n else 0.0,
        "avg_days_held": round(statistics.fmean(float(t["days_held"]) for t in trades), 1) if n else 0.0,
        "worst_trade": round(min(pnls), 2) if n else 0.0,
        "exit_reasons": reasons,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd / cfg.capital, 4) if cfg.capital else None,
        "sharpe_annual": round(sharpe, 3),
        "peak_margin": round(peak_margin, 2),
        "return_on_peak_margin": round(total / peak_margin, 4) if peak_margin > 0 else None,
        "sample_note": "noise" if n < 5 else ("thin" if n < 30 else "ok"),
        "fill_basis": fill_basis(cfg),
        "rule_timing": "signal and fill on the same session close (optimistic)",
    }


# -- entry point ------------------------------------------------------------------


def _entry_rule(
    conn: Any, symbols: Sequence[str], start: date, end: date, cfg: SimConfig
) -> tuple[dict[str, list[date]] | None, dict[str, Any]]:
    """Event dates per symbol when ``cfg.entry_event`` is set, and the rule as reported.

    The stub earnings calendar is refused: a position opened on an invented date
    would be a trade nobody could have made.
    """
    if not cfg.entry_event:
        return None, {"kind": "schedule", "every_sessions": cfg.entry_every_sessions}
    from bifrost_research.engines.backtest.event_defs import EventDef
    from bifrost_research.engines.backtest.event_query import resolve_events_between

    raw = dict(cfg.entry_event)
    params = {**dict(raw.get("params") or {}), "symbols": [str(s).strip().upper() for s in symbols]}
    event_def = EventDef.from_dict({"kind": raw.get("kind"), "params": params})
    resolved = resolve_events_between(conn, event_def, start, end)
    by_symbol: dict[str, list[date]] = {}
    if resolved.source != "stub":
        for sym, d in resolved.events:
            by_symbol.setdefault(live_label(sym), []).append(d)
    return by_symbol, {
        "kind": "event",
        "event_def": event_def.to_dict(),
        "offset_sessions": cfg.entry_offset_sessions,
        "source": resolved.source,
        "events": sum(len(v) for v in by_symbol.values()),
        "notes": resolved.notes or ("stub calendar refused: no real event dates" if resolved.source == "stub" else ""),
        "errors": list(resolved.errors),
    }



def run_sim(
    conn: Any,
    symbols: Sequence[str],
    start: date,
    end: date,
    cfg: SimConfig,
    *,
    stores: dict[str, ChainStore] | None = None,
) -> SimResult:
    """Run ``cfg`` over each symbol in [start, end] and aggregate.

    Symbols run independently (no shared margin limit yet — that is P4); the
    equity curve is the sum of their P&L on the shared session calendar.
    """
    if cfg.structure not in STRUCTURES:
        raise ValueError(f"unknown structure {cfg.structure!r}; available: {sorted(STRUCTURES)}")
    if end < start:
        raise ValueError("end must not be before start")
    all_trades: list[dict[str, Any]] = []
    curves: list[dict[date, tuple[float, float, int]]] = []
    skips: dict[str, int] = {}
    per_symbol: dict[str, dict[str, Any]] = {}
    events_by_symbol, entry_rule = _entry_rule(conn, symbols, start, end, cfg)
    for raw in symbols:
        sym = str(raw).strip().upper()
        if not sym:
            continue
        store = (stores or {}).get(sym) or ChainStore.load(conn, sym, start, end, max_dte=cfg.target_dte)
        if not store.sessions:
            skips["no_stock"] = skips.get("no_stock", 0) + 1
            per_symbol[sym] = {"n_trades": 0, "skipped": "no_stock"}
            continue
        if store.delisted_on is None and conn is not None:
            store.delisted_on = listing_end(conn, sym, as_of=date.today())
        events = None if events_by_symbol is None else events_by_symbol.get(live_label(sym), [])
        trades, curve, sk = _run_symbol(store, start, end, cfg, events=events)
        for k, v in sk.items():
            skips[k] = skips.get(k, 0) + v
        all_trades.extend(trades)
        curves.append(curve)
        per_symbol[sym] = {
            "n_trades": len(trades),
            "total_pnl": round(sum(t["pnl"] for t in trades), 2),
            "skipped_entries": sk,
        }
    all_trades.sort(key=lambda t: (t["entry_date"], t["symbol"]))
    for i, t in enumerate(all_trades, start=1):
        t["seq"] = i

    days = sorted({d for c in curves for d in c})
    equity: list[dict[str, Any]] = []
    last_seen: list[tuple[float, float, int]] = [(0.0, 0.0, 0) for _ in curves]
    for d in days:
        for i, c in enumerate(curves):
            if d in c:
                last_seen[i] = c[d]
        pnl = sum(x[0] for x in last_seen)
        equity.append(
            {
                "as_of": d.isoformat(),
                "equity": round(cfg.capital + pnl, 2),
                "margin_used": round(sum(x[1] for x in last_seen), 2),
                "open_positions": sum(x[2] for x in last_seen),
            }
        )
    summary = summarize(all_trades, equity, cfg)
    summary["skipped_entries"] = skips
    summary["per_symbol"] = per_symbol
    summary["window"] = {"start": start.isoformat(), "end": end.isoformat()}
    summary["entry_rule"] = entry_rule
    summary["evaluation"] = evaluation("option_simulator")
    summary["advisory"] = "D10 BLOCKED — historical replay only"
    return SimResult(summary=summary, trades=all_trades, equity=equity, params=cfg.to_dict())


__all__ = ["SimResult", "run_sim", "summarize"]
