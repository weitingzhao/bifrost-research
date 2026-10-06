"""Walk given legs from an entry session to their exit (suggestion settlement).

``run_sim`` picks its own legs on a schedule. A suggestion arrives with its legs
already chosen — the contracts, the sides, the management rules — and asks one
question: from the entry session, where does this position end? This entry
answers it with the simulator's own pieces (``_manage`` / ``_close``, the tiered
fill model, the margin model), so a settled suggestion and a simulator trade
are priced the same way.

The walk is one of three things:

- ``settled``: an exit was reached (rule, expiry, or the listing's end);
- ``void``: the position could not have been opened — a leg has no print on
  the entry session, or the entry session is not a session at all;
- ``open``: the data stops before an exit; walk again when more sessions land.

D10 BLOCKED — historical replay only; nothing here can reach an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Literal, Mapping

from bifrost_research.engines.adjusted_contracts import not_adjusted_contract_sql
from bifrost_research.engines.backtest.sim.chain import ChainStore, OptBar, _parse_occ
from bifrost_research.engines.backtest.sim.engine import _close, _Leg, _manage, _margin, _Position
from bifrost_research.engines.backtest.sim.rules import PriceField, SimConfig, fill_price
from bifrost_research.repositories.listing_lineage import live_label, stock_clause

WalkStatus = Literal["settled", "void", "open"]


@dataclass(frozen=True)
class LegSpec:
    ticker: str
    right: Literal["C", "P"]
    side: Literal["buy", "sell"]
    strike: float
    expiry: date
    qty: int = 1
    label: str = ""


@dataclass(frozen=True)
class WalkRules:
    """A position's management rules plus the pricing it settles on.

    ``profit_take_pct`` and ``stop_loss_mult`` are shares of the premium at
    stake (the credit, or the debit for a bought structure). ``max_hold_days``
    closes after that many calendar days whatever else holds.
    """

    profit_take_pct: float | None = 0.5
    stop_loss_mult: float | None = None
    dte_exit: int | None = 21
    max_hold_days: int | None = None
    max_stale_sessions: int | None = 3
    price_field: PriceField = "vwap"
    slippage_scale: float = 1.0
    commission_per_contract: float = 0.65
    multiplier: int = 100

    def sim_config(self, structure: str) -> SimConfig:
        return SimConfig(
            structure=structure,
            profit_take_pct=self.profit_take_pct,
            stop_loss_mult=self.stop_loss_mult,
            dte_exit=self.dte_exit,
            max_stale_sessions=self.max_stale_sessions,
            price_field=self.price_field,
            slippage_scale=self.slippage_scale,
            commission_per_contract=self.commission_per_contract,
            multiplier=self.multiplier,
        )


@dataclass
class WalkOutcome:
    status: WalkStatus
    reason: str = ""
    trade: dict[str, Any] = field(default_factory=dict)
    entry_slippage: float = 0.0
    exit_slippage: float = 0.0
    commission: float = 0.0

    @property
    def gross_pnl(self) -> float | None:
        """P&L before slippage and commission (fills at the mark)."""
        if self.status != "settled":
            return None
        return float(self.trade["pnl"]) + self.entry_slippage + self.exit_slippage + self.commission


def _d(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def load_leg_store(conn: Any, symbol: str, tickers: Iterable[str], start: date, end: date) -> ChainStore:
    """The underlying's sessions and the given contracts' bars only (option_daily,
    then the 16:00 snapshot for sessions it lacks).

    Settling a suggestion needs its own legs, not the whole chain — SPY's chain
    is thousands of contracts a session. Rates are left out: nothing on a walk
    solves IV.
    """
    sym = live_label(symbol)
    clause, sym_params = stock_clause(conn, sym)
    wanted = sorted({str(t) for t in tickers if t})
    spot: dict[date, float] = {}
    bars: list[OptBar] = []
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT bar_date, COALESCE(close_unadjusted, close)
            FROM raw_market.stock_daily
            WHERE {clause}
              AND bar_date BETWEEN %s AND %s
              AND close > 0
            ORDER BY bar_date
            """,
            (*sym_params, start, end),
        )
        for r in cur.fetchall() or []:
            d = _d(r[0])
            if d is not None and r[1] is not None and float(r[1]) > 0:
                spot[d] = float(r[1])
        if wanted:
            cur.execute(
                f"""
                SELECT option_ticker, expiry, strike, option_right, bar_date, close, vwap, volume
                FROM raw_market.option_daily
                WHERE underlying = %s
                  AND option_ticker = ANY(%s)
                  AND bar_date BETWEEN %s AND %s
                  AND close > 0
                  AND {not_adjusted_contract_sql("option_ticker")}
                """,
                (sym, wanted, start, end),
            )
            for r in cur.fetchall() or []:
                right = str(r[3] or "").strip().upper()[:1]
                exp, bd = _d(r[1]), _d(r[4])
                if right not in ("C", "P") or exp is None or bd is None:
                    continue
                bars.append(
                    OptBar(
                        ticker=str(r[0]),
                        expiry=exp,
                        strike=float(r[2]),
                        right=right,  # type: ignore[arg-type]
                        bar_date=bd,
                        close=float(r[5]),
                        vwap=float(r[6]) if r[6] is not None else None,
                        volume=int(r[7]) if r[7] is not None else None,
                    )
                )
    have = {(b.ticker, b.bar_date) for b in bars}
    bars += snapshot_day_bars(conn, sym, start, end, tickers=wanted, have=have)
    return ChainStore(sym, spot, bars)


def snapshot_day_bars(
    conn: Any,
    underlying: str,
    start: date,
    end: date,
    *,
    tickers: Iterable[str] | None = None,
    have: set[tuple[str, date]] | None = None,
) -> list[OptBar]:
    """Day bars from the 16:00 ET option snapshot, for (contract, session) pairs
    ``option_daily`` does not have.

    Since mid-August 2026 ``option_daily`` keeps about ten strikes either side of
    spot per expiry (SPY 2026-10-05: 765–785 against a 775 spot), so a 30-delta
    put 45 days out is not in it. The resident names' 16:00 snapshot carries the
    whole chain, and its day close / vwap / volume equal ``option_daily``'s row
    wherever both exist (checked on SPY 2026-10-02 and 10-05). Earlier snapshots
    of the day are partial sessions and are not used.
    """
    wanted = sorted({str(t) for t in tickers}) if tickers is not None else None
    if wanted is not None and not wanted:
        return []
    have = have or set()
    sql = """
        SELECT option_ticker, (snapshot_ts AT TIME ZONE 'America/New_York')::date AS d,
               day_close, day_vwap, day_volume
        FROM raw_market.option_snapshot
        WHERE underlying = %s
          AND snapshot_ts >= %s AND snapshot_ts < %s
          AND (snapshot_ts AT TIME ZONE 'America/New_York')::time >= '16:00'
          AND day_close > 0
    """
    params: list[Any] = [underlying, start, end + timedelta(days=2)]
    if wanted is not None:
        sql += " AND option_ticker = ANY(%s)"
        params.append(wanted)
    out: dict[tuple[str, date], OptBar] = {}
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        for r in cur.fetchall() or []:
            ticker, d = str(r[0]), _d(r[1])
            if d is None or not (start <= d <= end) or (ticker, d) in have:
                continue
            parsed = _parse_occ(ticker)
            if parsed is None:
                continue
            expiry, right, strike = parsed
            out[(ticker, d)] = OptBar(
                ticker=ticker,
                expiry=expiry,
                strike=strike,
                right=right,  # type: ignore[arg-type]
                bar_date=d,
                close=float(r[2]),
                vwap=float(r[3]) if r[3] is not None else None,
                volume=int(r[4]) if r[4] is not None else None,
                source="snapshot",
            )
    return list(out.values())


def with_snapshot_fill(conn: Any, store: ChainStore, start: date, end: date) -> ChainStore:
    """``store`` plus snapshot day bars for [start, end] where option_daily has none."""
    bars = [b for by_day in store._by_ticker.values() for b in by_day.values()]
    have = {(b.ticker, b.bar_date) for b in bars}
    extra = snapshot_day_bars(conn, store.symbol, start, end, have=have)
    if not extra:
        return store
    out = ChainStore(store.symbol, store.spot, bars + extra, store._rates)
    out.delisted_on = store.delisted_on
    return out


def _slip_cost(px: float, side: Literal["buy", "sell"], scale: float) -> float:
    return abs(fill_price(px, side, scale) - px)


def walk_legs(
    store: ChainStore,
    legs: list[LegSpec],
    entry_session: date,
    rules: WalkRules,
    *,
    structure: str = "custom",
) -> WalkOutcome:
    """Open ``legs`` at ``entry_session``'s prints and manage them to an exit."""
    if not legs:
        return WalkOutcome("void", "no_legs")
    if entry_session not in store.spot:
        return WalkOutcome("void", "entry_not_a_session")
    cfg = rules.sim_config(structure)
    opened: list[_Leg] = []
    entry_slip = 0.0
    for spec in legs:
        bar = store.bar(spec.ticker, entry_session)
        if bar is None:
            return WalkOutcome("void", f"no_entry_bar:{spec.ticker}")
        mark = bar.price(cfg.price_field)
        entry_slip += _slip_cost(mark, spec.side, cfg.slippage_scale) * spec.qty * cfg.multiplier
        opened.append(
            _Leg(
                label=spec.label or f"{spec.side} {spec.right}",
                ticker=spec.ticker,
                right=spec.right,
                side=spec.side,
                strike=float(spec.strike),
                expiry=spec.expiry,
                qty=int(spec.qty),
                entry_mark=mark,
                entry_fill=fill_price(mark, spec.side, cfg.slippage_scale),
                mark=mark,
            )
        )
    expiry = min(lg.expiry for lg in opened)
    pos = _Position(
        symbol=store.symbol,
        structure=structure,
        entry_date=entry_session,
        expiry=expiry,
        legs=opened,
        mult=cfg.multiplier,
        commission=cfg.commission_per_contract * sum(lg.qty for lg in opened),
        margin=0.0,
        max_loss=None,
    )
    pos.margin, pos.max_loss = _margin(structure, opened, store.spot[entry_session], pos.credit(), cfg.multiplier)
    pos.path.append(pos.open_pnl())
    entry_commission = pos.commission

    done: dict[str, Any] | None = None
    for d in store.sessions:
        if d <= entry_session:
            continue
        done = _manage(pos, store, d, cfg)
        if done is None and rules.max_hold_days is not None and (d - entry_session).days >= rules.max_hold_days:
            done = _close(pos, d, "max_hold", cfg)
        if done is not None:
            break
    if done is None:
        last = store.sessions[-1]
        if store.delisted_on is not None and last == store.delisted_on and last > entry_session:
            done = _close(pos, last, "delisted", cfg)
        else:
            return WalkOutcome("open", "no_exit_yet")

    expired = done["exit_reason"] in ("expiry", "expiry_itm")
    exit_slip = 0.0
    exit_commission = 0.0
    if not expired:
        for lg in pos.legs:
            closing = "sell" if lg.side == "buy" else "buy"
            exit_slip += _slip_cost(lg.mark, closing, cfg.slippage_scale) * lg.qty * cfg.multiplier
            exit_commission += cfg.commission_per_contract * lg.qty
    return WalkOutcome(
        "settled",
        done["exit_reason"],
        trade=done,
        entry_slippage=round(entry_slip, 2),
        exit_slippage=round(exit_slip, 2),
        commission=round(entry_commission + exit_commission, 2),
    )


def legs_from_json(rows: Iterable[Mapping[str, Any]]) -> list[LegSpec]:
    """Leg specs from a suggestion's ``legs_json``; raises on an incomplete leg."""
    out: list[LegSpec] = []
    for i, r in enumerate(rows):
        exp = r.get("expiry")
        expiry = exp if isinstance(exp, date) else date.fromisoformat(str(exp))
        right = str(r["right"]).upper()
        side = str(r["side"]).lower()
        if right not in ("C", "P") or side not in ("buy", "sell"):
            raise ValueError(f"leg {i}: right={right!r} side={side!r}")
        out.append(
            LegSpec(
                ticker=str(r["contract_key"]),
                right=right,  # type: ignore[arg-type]
                side=side,  # type: ignore[arg-type]
                strike=float(r["strike"]),
                expiry=expiry,
                qty=int(r.get("ratio") or 1),
                label=str(r.get("label") or ""),
            )
        )
    return out


__all__ = [
    "LegSpec",
    "WalkOutcome",
    "WalkRules",
    "legs_from_json",
    "load_leg_store",
    "snapshot_day_bars",
    "walk_legs",
    "with_snapshot_fill",
]
