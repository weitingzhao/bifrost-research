"""Event-driven backtest query engine (Wave RS-C1).

``run_event_query(event_def, template_name, lookback_years, ...)``:

1. Resolve the event definition into a set of ``(symbol, event_date)`` pairs
   over the requested lookback window.
2. For each event, build the strategy template legs, price them against
   ``raw_market.stock_daily`` / ``raw_market.option_daily`` at entry_date /
   exit_date, and compute per-run P&L.
3. Aggregate a summary (n_events, win_rate, avg/median P&L, Sharpe,
   max drawdown, MFE / MAE bounds).

D10 BLOCKED — pure historical replay, no execution path is ever reached.

Notes on data source gaps (Wave RS-C1):

- ``raw_market.option_daily`` currently ships OHLCV only (no bid/ask). The
  engine therefore prices legs against ``close`` prices by default. RS-C2's
  ``fills.compute_fill_price`` layers a mid ± slippage model on top and
  degrades back to ``close`` when bid/ask are unavailable — keeping this
  entry point backward compatible.
- No earnings calendar table exists in Golden Source. The ``earnings`` event
  resolver falls back through: (a) ``raw_market.corporate_action`` (only if
  it grows an ``earnings`` action_type — currently only splits/dividends);
  (b) ``features.event_signal_radar_daily`` heuristics; and (c) a small
  hard-coded stub for a canonical universe. The response ``summary`` and
  each event record advertise which source produced the dates.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from bifrost_research.engines.backtest.event_defs import EventDef
from bifrost_research.engines.backtest.strategy_templates import (
    LegSpec,
    build_legs,
    iter_legs,
    leg_signs,
    resolve_leg_window,
)
from bifrost_research.engines.opex_cycle.calendar import third_friday

logger = logging.getLogger(__name__)

# Canonical universe used for the earnings stub when no calendar table exists.
_STUB_EARNINGS_UNIVERSE: tuple[str, ...] = (
    "NVDA",
    "AAPL",
    "AMZN",
    "GOOGL",
    "MSFT",
    "META",
    "TSLA",
    "AMD",
    "SPY",
)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LegPricing:
    label: str
    kind: str  # option | stock
    side: str  # buy | sell
    quantity: int
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    strike: float | None = None
    expiry: str | None = None
    option_right: str | None = None
    pnl: float = 0.0
    contract_multiplier: int = 1
    fill_details: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventRun:
    event_date: str
    symbol: str
    entry_ts: str
    exit_ts: str
    pnl: float
    mfe: float
    mae: float
    legs: list[LegPricing] = field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Event resolvers
# ---------------------------------------------------------------------------


@dataclass
class ResolvedEvents:
    events: list[tuple[str, date]]  # (symbol, event_date)
    source: str  # "stub" | "corporate_action" | "event_radar" | "opex" | "sepa" | "iv" | "sql"
    notes: str = ""


def _lookback_window(lookback_years: int, today: date | None = None) -> tuple[date, date]:
    end = today or date.today()
    years = max(1, int(lookback_years))
    start = date(end.year - years, end.month, min(end.day, 28))
    return start, end


def _params_symbols(params: Mapping[str, Any]) -> list[str]:
    raw = params.get("symbols") or params.get("symbol")
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.split(",")]
    symbols: list[str] = []
    seen: set[str] = set()
    for item in raw:
        s = str(item).strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        symbols.append(s)
    return symbols


def _resolve_earnings_events(
    conn: Any,
    params: Mapping[str, Any],
    start: date,
    end: date,
) -> ResolvedEvents:
    """Resolve earnings events using best-available Golden Source data.

    Priority:
      1. ``raw_market.corporate_action`` filtered to an ``earnings`` action_type
         (currently only split/dividend rows exist — this branch simply falls
         through when zero rows match).
      2. ``features.event_signal_radar_daily`` with ``raw_text`` / ``event_summary``
         ILIKE '%earnings%' or '%财报%'.
      3. Hard-coded stub with the trailing 8 quarters (roughly every ~91 days)
         for the canonical universe.
    """
    symbols = _params_symbols(params)
    universe = symbols or list(_STUB_EARNINGS_UNIVERSE)

    events: list[tuple[str, date]] = []

    # (1) corporate_action
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT UPPER(TRIM(symbol)), ex_date
                FROM raw_market.corporate_action
                WHERE action_type = 'earnings'
                  AND ex_date BETWEEN %s AND %s
                  AND (%s::text[] IS NULL OR UPPER(TRIM(symbol)) = ANY(%s::text[]))
                ORDER BY ex_date
                """,
                (start, end, universe or None, universe or None),
            )
            rows = cur.fetchall() or []
        for sym, ex_date in rows:
            if isinstance(ex_date, datetime):
                ex_date = ex_date.date()
            events.append((str(sym), ex_date))
        if events:
            return ResolvedEvents(events=events, source="corporate_action")
    except Exception as exc:  # pragma: no cover - depends on DB schema
        logger.debug("earnings corporate_action fallback: %s", exc)

    # (2) event_signal_radar_daily heuristic
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT UPPER(TRIM(affected_symbols)), collected_at
                FROM features.event_signal_radar_daily
                WHERE (raw_text ILIKE '%earnings%' OR event_summary ILIKE '%earnings%'
                       OR raw_text ILIKE '%财报%' OR event_summary ILIKE '%财报%')
                  AND collected_at BETWEEN %s AND %s
                  AND (dropped IS NULL OR dropped = false)
                """,
                (start, end),
            )
            rows = cur.fetchall() or []
        found: list[tuple[str, date]] = []
        for sym_raw, event_date in rows:
            sym = (sym_raw or "").split(",")[0].strip().upper()
            if not sym or (symbols and sym not in symbols):
                continue
            if isinstance(event_date, datetime):
                event_date = event_date.date()
            found.append((sym, event_date))
        if found:
            return ResolvedEvents(events=found, source="event_radar")
    except Exception as exc:  # pragma: no cover
        logger.debug("earnings event_radar fallback: %s", exc)

    # (3) Stub — quarterly cadence back from ``end`` for the universe.
    stub: list[tuple[str, date]] = []
    for sym in universe:
        d = end
        for _q in range(8):
            d = d - timedelta(days=91)
            if d < start:
                break
            stub.append((sym, d))
    return ResolvedEvents(
        events=stub,
        source="stub",
        notes=(
            "earnings source: stub — replace when a real earnings calendar is "
            "wired to Golden Source"
        ),
    )


def _resolve_opex_events(
    params: Mapping[str, Any],
    start: date,
    end: date,
) -> ResolvedEvents:
    symbols = _params_symbols(params) or ["SPY"]
    events: list[tuple[str, date]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        try:
            opex = third_friday(year, month)
        except Exception:  # pragma: no cover
            opex = None
        if opex is not None and start <= opex <= end:
            for sym in symbols:
                events.append((sym, opex))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return ResolvedEvents(events=events, source="opex")


def _resolve_sepa_hit_events(
    conn: Any,
    params: Mapping[str, Any],
    start: date,
    end: date,
) -> ResolvedEvents:
    symbols = _params_symbols(params)
    threshold = float(params.get("threshold") or 0.7)
    score_col = str(params.get("score_col") or "sepa_score")
    if score_col not in {
        "sepa_score",
        "fundamental_score",
        "trend_template_score",
        "momentum_score",
        "structure_score",
    }:
        raise ValueError(f"invalid sepa score_col: {score_col!r}")
    where_extra = "AND UPPER(TRIM(symbol)) = ANY(%s::text[])" if symbols else ""
    params_tuple: list[Any] = [start, end, threshold]
    if symbols:
        params_tuple.append(symbols)
    sql = f"""
        SELECT UPPER(TRIM(symbol)), trade_date
        FROM features.stock_signal_sepa_daily
        WHERE trade_date BETWEEN %s AND %s
          AND {score_col} >= %s
          {where_extra}
        ORDER BY trade_date, symbol
    """
    events: list[tuple[str, date]] = []
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params_tuple))
            rows = cur.fetchall() or []
        for sym, td in rows:
            if isinstance(td, datetime):
                td = td.date()
            events.append((str(sym), td))
    except Exception as exc:  # pragma: no cover
        logger.debug("sepa_hit resolver failed: %s", exc)
    return ResolvedEvents(
        events=events,
        source="sepa",
        notes=(
            "sepa historical coverage is limited — features.stock_signal_sepa_daily "
            "is daily-UPSERT overwrite (see schema notes)."
        ),
    )


def _resolve_iv_percentile_events(
    conn: Any,
    params: Mapping[str, Any],
    start: date,
    end: date,
) -> ResolvedEvents:
    symbols = _params_symbols(params)
    threshold = float(params.get("threshold") or 80.0)
    direction = str(params.get("direction") or "above").lower()
    if direction not in {"above", "below"}:
        raise ValueError(f"iv_percentile direction must be 'above'/'below', got {direction!r}")
    op = ">=" if direction == "above" else "<="
    where_extra = "AND UPPER(TRIM(symbol)) = ANY(%s::text[])" if symbols else ""
    params_tuple: list[Any] = [start, end, threshold]
    if symbols:
        params_tuple.append(symbols)
    sql = f"""
        SELECT UPPER(TRIM(symbol)), trade_date
        FROM features.option_metric_iv_percentile_daily
        WHERE trade_date BETWEEN %s AND %s
          AND iv_percentile_1y {op} %s
          {where_extra}
        ORDER BY trade_date, symbol
    """
    events: list[tuple[str, date]] = []
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params_tuple))
            rows = cur.fetchall() or []
        for sym, td in rows:
            if isinstance(td, datetime):
                td = td.date()
            events.append((str(sym), td))
    except Exception as exc:  # pragma: no cover
        logger.debug("iv_percentile resolver failed: %s", exc)
    return ResolvedEvents(events=events, source="iv")


def _resolve_sql_events(
    conn: Any,
    params: Mapping[str, Any],
    start: date,
    end: date,
) -> ResolvedEvents:  # pragma: no cover - v1 not implemented
    raise NotImplementedError(
        "EventDef.kind='sql' is not implemented in Wave RS-C1. "
        "Use one of: earnings, opex, sepa_hit, iv_percentile_threshold."
    )


def resolve_events(
    conn: Any,
    event_def: EventDef,
    lookback_years: int,
    *,
    today: date | None = None,
) -> ResolvedEvents:
    start, end = _lookback_window(lookback_years, today=today)
    kind = event_def.kind
    params = event_def.params or {}
    if kind == "earnings":
        return _resolve_earnings_events(conn, params, start, end)
    if kind == "opex":
        return _resolve_opex_events(params, start, end)
    if kind == "sepa_hit":
        return _resolve_sepa_hit_events(conn, params, start, end)
    if kind == "iv_percentile_threshold":
        return _resolve_iv_percentile_events(conn, params, start, end)
    if kind == "sql":
        return _resolve_sql_events(conn, params, start, end)
    raise ValueError(f"unknown event kind {kind!r}")


# ---------------------------------------------------------------------------
# Price lookups
# ---------------------------------------------------------------------------


def _fetch_stock_price(conn: Any, symbol: str, on_or_before: date) -> dict[str, Any] | None:
    """Return {bar_date, open, close} on or before ``on_or_before`` (latest)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bar_date, open, close
                FROM raw_market.stock_daily
                WHERE UPPER(TRIM(symbol)) = %s
                  AND bar_date <= %s
                ORDER BY bar_date DESC
                LIMIT 1
                """,
                (symbol.strip().upper(), on_or_before),
            )
            row = cur.fetchone()
    except Exception as exc:  # pragma: no cover
        logger.debug("stock lookup failed for %s@%s: %s", symbol, on_or_before, exc)
        return None
    if row is None:
        return None
    if isinstance(row, Mapping):
        return {
            "bar_date": row.get("bar_date"),
            "open": row.get("open"),
            "close": row.get("close"),
        }
    return {"bar_date": row[0], "open": row[1], "close": row[2]}


def _pick_option(
    conn: Any,
    symbol: str,
    on_or_before: date,
    right: str,
    target_dte: int,
    strike_target: float,
    tolerance_pct: float = 0.30,
) -> dict[str, Any] | None:
    """Pick the option_daily bar closest to (target expiry, target strike).

    Uses ``option_daily.close`` for pricing. Strike selection prefers the
    nearest strike within ``tolerance_pct`` of ``strike_target`` on the
    expiry closest to ``on_or_before + target_dte``.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT expiry, strike, close, open, high, low, bar_date, option_ticker
                FROM raw_market.option_daily
                WHERE UPPER(TRIM(underlying)) = %s
                  AND option_right = %s
                  AND bar_date <= %s
                  AND expiry > %s
                ORDER BY bar_date DESC
                LIMIT 400
                """,
                (symbol.strip().upper(), right, on_or_before, on_or_before),
            )
            rows = cur.fetchall() or []
    except Exception as exc:  # pragma: no cover
        logger.debug("option lookup failed for %s@%s: %s", symbol, on_or_before, exc)
        return None
    if not rows:
        return None
    target_expiry = on_or_before + timedelta(days=max(1, int(target_dte)))

    def _to_dict(r: Any) -> dict[str, Any]:
        if isinstance(r, Mapping):
            return dict(r)
        return {
            "expiry": r[0],
            "strike": float(r[1] or 0),
            "close": float(r[2] or 0),
            "open": float(r[3] or 0) if r[3] is not None else None,
            "high": float(r[4] or 0) if r[4] is not None else None,
            "low": float(r[5] or 0) if r[5] is not None else None,
            "bar_date": r[6],
            "option_ticker": r[7],
        }

    candidates = [_to_dict(r) for r in rows]

    # narrow to nearest expiry
    expiries = sorted({c["expiry"] for c in candidates if c.get("expiry")})
    if not expiries:
        return None
    best_expiry = min(expiries, key=lambda e: abs((e - target_expiry).days))
    same_expiry = [c for c in candidates if c["expiry"] == best_expiry]

    # narrow to nearest strike within tolerance
    band = abs(strike_target) * tolerance_pct
    within = [c for c in same_expiry if abs(c["strike"] - strike_target) <= max(band, 0.01)]
    pool = within or same_expiry
    if not pool:
        return None
    return min(pool, key=lambda c: abs(c["strike"] - strike_target))


def _price_stock_leg(
    conn: Any,
    symbol: str,
    entry_date: date,
    exit_date: date,
    leg: LegSpec,
) -> LegPricing | None:
    entry = _fetch_stock_price(conn, symbol, entry_date)
    exit_ = _fetch_stock_price(conn, symbol, exit_date)
    if not entry or not exit_ or entry["close"] is None or exit_["close"] is None:
        return None
    entry_px = float(entry["close"])
    exit_px = float(exit_["close"])
    sign = leg_signs(leg)
    pnl = sign * (exit_px - entry_px) * leg.quantity
    return LegPricing(
        label=leg.label or "stock",
        kind="stock",
        side=leg.side,
        quantity=leg.quantity,
        entry_date=entry_date.isoformat(),
        exit_date=exit_date.isoformat(),
        entry_price=round(entry_px, 6),
        exit_price=round(exit_px, 6),
        pnl=round(pnl, 6),
        contract_multiplier=1,
        fill_details={"pricing_source": "stock_close"},
    )


def _price_option_leg(
    conn: Any,
    symbol: str,
    entry_date: date,
    exit_date: date,
    leg: LegSpec,
    *,
    fill_config: Any | None = None,
) -> LegPricing | None:
    stock_entry = _fetch_stock_price(conn, symbol, entry_date)
    if not stock_entry or stock_entry.get("close") is None:
        return None
    spot = float(stock_entry["close"])

    # Strike selection — RS-C1 v1 uses moneyness_offset; target_delta reserved
    # for future work when delta / IV columns are available in option_daily.
    strike_target = spot * (1.0 + float(leg.target_moneyness_offset))

    entry_option = _pick_option(
        conn,
        symbol=symbol,
        on_or_before=entry_date,
        right=leg.option_right or "C",
        target_dte=leg.target_dte,
        strike_target=strike_target,
    )
    if not entry_option:
        return None
    # Reuse same contract at exit — pick by expiry+strike on exit_date
    exit_option = _pick_option(
        conn,
        symbol=symbol,
        on_or_before=exit_date,
        right=leg.option_right or "C",
        target_dte=max(1, (entry_option["expiry"] - exit_date).days),
        strike_target=float(entry_option["strike"]),
        tolerance_pct=0.01,
    )
    if not exit_option:
        return None

    # Prices — RS-C1 defaults to close. RS-C2 fills.compute_fill_price will
    # layer mid ± slippage when bid/ask columns are populated.
    entry_price = _apply_fill(leg, entry_option, side=leg.side, fill_config=fill_config)
    exit_side = "sell" if leg.side == "buy" else "buy"
    exit_price = _apply_fill(leg, exit_option, side=exit_side, fill_config=fill_config)

    sign = leg_signs(leg)
    contract_mult = int(getattr(fill_config, "multiplier", 100) if fill_config is not None else 100)
    gross = sign * (exit_price - entry_price) * leg.quantity * contract_mult
    commission = 0.0
    if fill_config is not None:
        commission = float(getattr(fill_config, "commission_per_contract", 0.0)) * leg.quantity * 2
    pnl = gross - commission
    fill_details = {
        "pricing_source": "option_close",
        "strike_target": round(strike_target, 6),
        "entry_bar": entry_option.get("bar_date").isoformat()
        if isinstance(entry_option.get("bar_date"), date)
        else str(entry_option.get("bar_date")),
        "exit_bar": exit_option.get("bar_date").isoformat()
        if isinstance(exit_option.get("bar_date"), date)
        else str(exit_option.get("bar_date")),
        "commission": round(commission, 6),
    }
    return LegPricing(
        label=leg.label or "option",
        kind="option",
        side=leg.side,
        quantity=leg.quantity,
        entry_date=entry_date.isoformat(),
        exit_date=exit_date.isoformat(),
        entry_price=round(entry_price, 6),
        exit_price=round(exit_price, 6),
        strike=float(entry_option["strike"]),
        expiry=(
            entry_option["expiry"].isoformat()
            if isinstance(entry_option.get("expiry"), date)
            else str(entry_option.get("expiry"))
        ),
        option_right=leg.option_right,
        pnl=round(pnl, 6),
        contract_multiplier=contract_mult,
        fill_details=fill_details,
    )


def _apply_fill(
    leg: LegSpec,
    bar: Mapping[str, Any],
    *,
    side: str,
    fill_config: Any | None,
) -> float:
    """Compute the fill price. Delegates to RS-C2 ``fills.compute_fill_price``
    when a ``FillConfig`` is supplied; otherwise falls back to the bar close.
    """
    close = float(bar.get("close") or 0.0)
    if fill_config is None:
        return close
    # Late import to avoid a hard dependency for RS-C1 users.
    from bifrost_research.engines.backtest.fills import compute_fill_price  # noqa: WPS433

    bid = float(bar.get("bid") or 0.0)
    ask = float(bar.get("ask") or 0.0)
    return compute_fill_price(side=side, bid=bid, ask=ask, close=close, config=fill_config)


# ---------------------------------------------------------------------------
# MFE / MAE + summary aggregation
# ---------------------------------------------------------------------------


def _mfe_mae_for_run(
    conn: Any,
    symbol: str,
    entry_date: date,
    exit_date: date,
    direction_sign: int,
) -> tuple[float, float]:
    """Rough MFE/MAE proxy from stock high/low path (percent of entry close).

    Positive MFE = best move in the run's favor; negative MAE = worst move.
    Bounded so tests can assert MFE >= 0 and MAE <= 0.
    """
    if exit_date < entry_date:
        entry_date, exit_date = exit_date, entry_date
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bar_date, high, low, close
                FROM raw_market.stock_daily
                WHERE UPPER(TRIM(symbol)) = %s
                  AND bar_date BETWEEN %s AND %s
                ORDER BY bar_date
                """,
                (symbol.strip().upper(), entry_date, exit_date),
            )
            rows = cur.fetchall() or []
    except Exception:  # pragma: no cover
        return 0.0, 0.0
    if not rows:
        return 0.0, 0.0

    def _val(row: Any, idx: int, key: str) -> Any:
        return row.get(key) if isinstance(row, Mapping) else row[idx]

    entry_close = float(_val(rows[0], 3, "close") or 0.0)
    if entry_close == 0:
        return 0.0, 0.0
    highs = [float(_val(r, 1, "high") or _val(r, 3, "close") or entry_close) for r in rows]
    lows = [float(_val(r, 2, "low") or _val(r, 3, "close") or entry_close) for r in rows]
    top = (max(highs) - entry_close) / entry_close
    bot = (min(lows) - entry_close) / entry_close
    if direction_sign >= 0:
        mfe, mae = max(top, 0.0), min(bot, 0.0)
    else:
        mfe, mae = max(-bot, 0.0), min(-top, 0.0)
    return round(mfe, 6), round(mae, 6)


def _direction_sign(legs: Sequence[LegSpec]) -> int:
    """+1 for net-bullish, -1 for net-bearish, 0 for neutral (straddle-like)."""
    net = 0
    for leg in legs:
        if leg.kind == "stock":
            net += leg_signs(leg) * leg.quantity
        elif leg.option_right == "C":
            net += leg_signs(leg)
        elif leg.option_right == "P":
            net -= leg_signs(leg)
    if net > 0:
        return 1
    if net < 0:
        return -1
    return 0


def _summarize(runs: Sequence[EventRun]) -> dict[str, Any]:
    if not runs:
        return {
            "n_events": 0,
            "win_rate": 0.0,
            "avg_pnl": 0.0,
            "median_pnl": 0.0,
            "sharpe_annual": 0.0,
            "max_drawdown": 0.0,
            "avg_mfe": 0.0,
            "avg_mae": 0.0,
        }
    pnls = [r.pnl for r in runs]
    wins = sum(1 for p in pnls if p > 0)
    n = len(pnls)
    mean = sum(pnls) / n
    stdev = statistics.pstdev(pnls) if n > 1 else 0.0
    # Rough annualization: assume runs are roughly one per week (~52/y).
    sharpe = (mean / stdev * math.sqrt(52)) if stdev > 0 else 0.0
    median = statistics.median(pnls)
    # Max drawdown on the cumulative P&L curve.
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    avg_mfe = statistics.fmean(r.mfe for r in runs)
    avg_mae = statistics.fmean(r.mae for r in runs)
    return {
        "n_events": n,
        "win_rate": round(wins / n, 4),
        "avg_pnl": round(mean, 6),
        "median_pnl": round(median, 6),
        "sharpe_annual": round(sharpe, 4),
        "max_drawdown": round(max_dd, 6),
        "avg_mfe": round(avg_mfe, 6),
        "avg_mae": round(avg_mae, 6),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_event_query(
    event_def: EventDef | Mapping[str, Any],
    template_name: str,
    *,
    lookback_years: int = 3,
    conn: Any | None = None,
    fill_config: Any | None = None,
    today: date | None = None,
    max_events: int = 500,
    **template_kwargs: Any,
) -> dict[str, Any]:
    """Run an event-driven backtest and return ``{runs[], summary, ...}``.

    - ``event_def`` may be an ``EventDef`` or a dict; both are normalized.
    - ``conn`` may be omitted, in which case a Golden Source connection is
      opened for the duration of the call (opt-in for CLI use).
    - ``fill_config`` (RS-C2) plumbs realistic fills through option leg
      pricing; when None, legs are priced at close.
    """
    if isinstance(event_def, Mapping):
        event_def = EventDef.from_dict(event_def)

    close_conn = False
    if conn is None:
        from bifrost_research.db.conn import connect  # local import

        conn = connect()
        close_conn = True

    try:
        resolved = resolve_events(conn, event_def, lookback_years, today=today)
        legs = iter_legs(build_legs(template_name, **template_kwargs))
        direction_sign = _direction_sign(legs)

        runs: list[EventRun] = []
        skipped = 0
        for symbol, event_date in resolved.events[: max(1, int(max_events))]:
            leg_pricings: list[LegPricing] = []
            entry_dates: list[date] = []
            exit_dates: list[date] = []
            skip_run = False
            for leg in legs:
                entry_date, exit_date = resolve_leg_window(leg, event_date)
                entry_dates.append(entry_date)
                exit_dates.append(exit_date)
                if leg.kind == "stock":
                    pricing = _price_stock_leg(conn, symbol, entry_date, exit_date, leg)
                else:
                    pricing = _price_option_leg(
                        conn, symbol, entry_date, exit_date, leg, fill_config=fill_config
                    )
                if pricing is None:
                    skip_run = True
                    break
                leg_pricings.append(pricing)
            if skip_run or not leg_pricings:
                skipped += 1
                continue
            entry_ts = min(entry_dates)
            exit_ts = max(exit_dates)
            pnl = sum(lp.pnl for lp in leg_pricings)
            mfe, mae = _mfe_mae_for_run(conn, symbol, entry_ts, exit_ts, direction_sign)
            runs.append(
                EventRun(
                    event_date=event_date.isoformat(),
                    symbol=symbol,
                    entry_ts=entry_ts.isoformat(),
                    exit_ts=exit_ts.isoformat(),
                    pnl=round(pnl, 6),
                    mfe=mfe,
                    mae=mae,
                    legs=leg_pricings,
                    notes="D10 BLOCKED — historical replay only",
                )
            )
    finally:
        if close_conn:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass

    summary = _summarize(runs)
    return {
        "runs": [_run_to_dict(r) for r in runs],
        "summary": summary,
        "event_source": resolved.source,
        "event_source_notes": resolved.notes,
        "skipped_events": skipped,
        "template": template_name,
        "template_kwargs": dict(template_kwargs),
        "event_def": event_def.to_dict(),
        "lookback_years": int(lookback_years),
        "advisory": "D10 BLOCKED — historical replay only",
    }


def _run_to_dict(run: EventRun) -> dict[str, Any]:
    d = asdict(run)
    d["legs"] = [asdict(lp) for lp in run.legs]
    return d


def summarize_runs(runs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Public helper — recompute summary from a list of run dicts."""
    event_runs: list[EventRun] = []
    for r in runs:
        legs = [LegPricing(**lp) for lp in (r.get("legs") or [])]
        event_runs.append(
            EventRun(
                event_date=r.get("event_date") or "",
                symbol=r.get("symbol") or "",
                entry_ts=r.get("entry_ts") or r.get("event_date") or "",
                exit_ts=r.get("exit_ts") or r.get("event_date") or "",
                pnl=float(r.get("pnl") or 0.0),
                mfe=float(r.get("mfe") or 0.0),
                mae=float(r.get("mae") or 0.0),
                legs=legs,
            )
        )
    return _summarize(event_runs)


__all__ = [
    "EventRun",
    "LegPricing",
    "ResolvedEvents",
    "resolve_events",
    "run_event_query",
    "summarize_runs",
]
