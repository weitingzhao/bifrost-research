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
- Earnings dates are the name's 8-K Item 2.02 filings (the results release),
  from ``raw_market.sec_8k_filing`` (0.171.0, B6; before that the 10-Q / 10-K
  filing date, weeks after the print). The resolver falls back through:
  (a) ``raw_market.corporate_action`` (only if it grows an ``earnings``
  action_type — currently only splits/dividends); (b)
  ``features.event_signal_radar_daily`` heuristics; and (c) a small hard-coded
  stub for a canonical universe. The response ``summary`` and each event record
  advertise which source produced the dates.
- ``raw_market.option_daily`` has covered 2024-10 onward since the backfill
  (each contract's last ~90 days before expiry, strikes within ±30% of spot);
  events outside that are skipped rather than priced. ``summary.skipped_no_option``
  / ``skipped_no_stock`` / ``skipped_incomplete_window`` carry the reason so a
  caller never reads a missing dataset as a losing strategy.
- Offsets count trading sessions (0.169.0); an option leg is opened on one
  contract and closed on that same contract, or settled at intrinsic if the
  exit reaches its expiry.
- Entry timing (0.175.0, ``summary.entry_timing`` version 2): for a signal
  event (``indicator_signal`` / ``pine_signal``) offset 0 is the session
  *after* the signal, because the signal only exists once its session has
  closed; every leg is priced at the entry session's close. Other kinds keep
  offset 0 = the first session on or after the event. Runs stored before
  0.175.0 carry no ``entry_timing`` and read offset 0 as the signal session.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from bifrost_research.engines.adjusted_contracts import not_adjusted_contract_sql
from bifrost_research.engines.backtest.catalog import evaluation
from bifrost_research.engines.backtest.event_defs import EventDef, entry_after_event, entry_timing
from bifrost_research.engines.backtest.strategy_templates import (
    LegSpec,
    build_legs,
    iter_legs,
    leg_signs,
    resolve_trading_window,
)
from bifrost_research.engines.opex_cycle.calendar import third_friday
from bifrost_research.repositories.earnings_filings import (
    RELEASE_WITHIN_DAYS,
    distinct_prints,
    speaks_of_results,
    split_releases,
)
from bifrost_research.repositories.listing_lineage import labels, listing_end, live_label, stock_clause

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
    source: str  # "sec_8k_item_2_02" | "stub" | "corporate_action" | "event_radar" | "opex" | "sepa" | "iv" | "indicator" | "pine" | "unavailable"
    notes: str = ""
    # A source that could not be read (permission, missing table), named with
    # its error. An empty result is not an error; a failed read never passes
    # for one.
    errors: list[str] = field(default_factory=list)


def _read_failed(conn: Any, what: str, exc: Exception, errors: list[str]) -> None:
    """Record a failed read and clear the aborted transaction behind it."""
    msg = " ".join(str(exc).split())[:200]
    errors.append(f"{what}: {type(exc).__name__}: {msg}")
    logger.warning("event source %s unreadable: %s", what, msg)
    _rollback(conn)


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


def _rollback(conn: Any) -> None:
    rollback = getattr(conn, "rollback", None)
    if callable(rollback):
        try:
            rollback()
        except Exception:  # noqa: BLE001, S110
            pass


def _earnings_prints_8k(
    conn: Any, universe: Sequence[str], start: date, end: date
) -> list[tuple[str, date]]:
    """(symbol, filing day) of each results release in [start, end], by date.

    Filings are read ``RELEASE_WITHIN_DAYS`` past ``end`` so a filing near the
    end is judged the same way as one in the middle: whether a 2.02 filing that
    says nothing about results is set aside depends on a release following it.
    Filings within ``SAME_PRINT_DAYS`` of a kept one (8-K/A, follow-ups) are the
    same print. A renamed company's filings under its old ticker count, and
    every event comes back under the ticker it trades as now.
    """
    tickers = sorted({t for sym in universe for t in labels(sym)})
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT UPPER(TRIM(symbol)), filing_date, items_text
            FROM raw_market.sec_8k_filing
            WHERE '2.02' = ANY(items)
              AND filing_date BETWEEN %s AND %s
              AND (%s::text[] IS NULL OR symbol = ANY(%s::text[]))
            """,
            (
                start - timedelta(days=RELEASE_WITHIN_DAYS),
                end + timedelta(days=RELEASE_WITHIN_DAYS),
                tickers or None,
                tickers or None,
            ),
        )
        rows = cur.fetchall() or []
    by_symbol: dict[str, list[tuple[date, bool]]] = {}
    for sym, filed, text in rows:
        if isinstance(filed, datetime):
            filed = filed.date()
        if not isinstance(filed, date):
            continue
        by_symbol.setdefault(live_label(str(sym)), []).append((filed, speaks_of_results(text)))
    out: list[tuple[str, date]] = []
    for sym, filings in by_symbol.items():
        kept, _aside = split_releases(filings)
        out.extend((sym, d) for d in distinct_prints(kept) if start <= d <= end)
    out.sort(key=lambda e: (e[1], e[0]))
    return out


def _resolve_earnings_events(
    conn: Any,
    params: Mapping[str, Any],
    start: date,
    end: date,
) -> ResolvedEvents:
    """Resolve earnings events using best-available Golden Source data.

    Priority:
      0. The name's own 8-K filings carrying Item 2.02 (results of operations),
         from ``raw_market.sec_8k_filing``, less the 2.02 filings that are not a
         results release (``repositories.earnings_filings``: Tesla's delivery
         reports and the like). The event date is the filing day: the release
         came before that day's open or after its close, so an entry at offset
         -1 or earlier is the last one surely before the print.
      1. ``raw_market.corporate_action`` filtered to an ``earnings`` action_type
         (currently only split/dividend rows exist — this branch simply falls
         through when zero rows match).
      2. ``features.event_signal_radar_daily`` with ``raw_text`` / ``event_summary``
         ILIKE '%earnings%' or '%财报%'.
      3. Hard-coded stub with the trailing 8 quarters (roughly every ~91 days)
         for the canonical universe.

    Until 0.171.0 rung 0 was ``raw_market.stock_financials.filing_date`` — when
    the 10-Q / 10-K reached EDGAR, 24–31 days after the results release on NVDA.
    An entry "one session before earnings" sat weeks after the print (B6). It is
    not a fallback either: a name without an 8-K on file is left out and named
    in ``notes`` rather than dated a month late.

    The stub stays as the last rung: a run with no real dates should say so
    rather than return nothing.
    """
    symbols = _params_symbols(params)
    universe = symbols or list(_STUB_EARNINGS_UNIVERSE)

    events: list[tuple[str, date]] = []
    errors: list[str] = []

    # (0) 8-K Item 2.02 — the results release.
    try:
        prints = _earnings_prints_8k(conn, universe, start, end)
        if prints:
            covered = {sym for sym, _d in prints}
            missing = [sym for sym in universe if live_label(sym) not in covered]
            notes = (
                "event_date is the 8-K Item 2.02 filing day; the release was before its open "
                "or after its close, so only entries at offset <= -1 are surely before the print"
            )
            if missing:
                notes += f"; no results 8-K on file in the window, left out: {', '.join(missing)}"
            return ResolvedEvents(events=prints, source="sec_8k_item_2_02", notes=notes)
    except Exception as exc:  # noqa: BLE001 — reported, not swallowed
        # The real calendar could not be read (a role without SELECT on
        # raw_market.sec_8k_filing, 2026-10-05). Falling on to the stub would
        # hand back invented dates and call the run an earnings backtest.
        _read_failed(conn, "raw_market.sec_8k_filing", exc, errors)
        return ResolvedEvents(
            events=[],
            source="unavailable",
            notes="earnings calendar unreadable — no events; see errors",
            errors=errors,
        )

    # (1) corporate_action
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT UPPER(TRIM(symbol)), ex_date
                FROM raw_market.corporate_action
                WHERE action_type = 'earnings'
                  AND ex_date BETWEEN %s AND %s
                  AND (%s::text[] IS NULL OR symbol = ANY(%s::text[]))
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
            return ResolvedEvents(events=events, source="corporate_action", errors=errors)
    except Exception as exc:  # noqa: BLE001
        _read_failed(conn, "raw_market.corporate_action", exc, errors)

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
            return ResolvedEvents(events=found, source="event_radar", errors=errors)
    except Exception as exc:  # noqa: BLE001
        _read_failed(conn, "features.event_signal_radar_daily", exc, errors)

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
        errors=errors,
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
    where_extra = "AND symbol = ANY(%s::text[])" if symbols else ""
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
    errors: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params_tuple))
            rows = cur.fetchall() or []
        for sym, td in rows:
            if isinstance(td, datetime):
                td = td.date()
            events.append((str(sym), td))
    except Exception as exc:  # noqa: BLE001
        _read_failed(conn, "features.stock_signal_sepa_daily", exc, errors)
    return ResolvedEvents(
        events=events,
        source="sepa" if not errors else "unavailable",
        errors=errors,
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
    where_extra = "AND symbol = ANY(%s::text[])" if symbols else ""
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
    errors: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params_tuple))
            rows = cur.fetchall() or []
        for sym, td in rows:
            if isinstance(td, datetime):
                td = td.date()
            events.append((str(sym), td))
    except Exception as exc:  # noqa: BLE001
        _read_failed(conn, "features.option_metric_iv_percentile_daily", exc, errors)
    return ResolvedEvents(events=events, source="iv" if not errors else "unavailable", errors=errors)


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


MAX_INDICATOR_SYMBOLS = 50


def _resolve_indicator_signal_events(
    conn: Any,
    params: Mapping[str, Any],
    start: date,
    end: date,
) -> ResolvedEvents:
    """Sessions where a standard indicator signal fired, per symbol, from daily closes.

    Computed on the fly from ``raw_market.stock_daily`` with a warm-up before
    ``start``; nothing is stored. Needs explicit symbols — scanning the whole
    universe bar by bar belongs in a batch job, not a request.
    """
    from bifrost_research.engines.indicators import get_signal, signal_dates
    from bifrost_research.engines.indicators.bars import load_bars

    spec = get_signal(str(params.get("signal") or ""))
    sig_params = spec.params(params)
    symbols = _params_symbols(params)
    if not symbols:
        raise ValueError("indicator_signal needs params.symbols")
    if len(symbols) > MAX_INDICATOR_SYMBOLS:
        raise ValueError(f"indicator_signal takes at most {MAX_INDICATOR_SYMBOLS} symbols")
    events: list[tuple[str, date]] = []
    errors: list[str] = []
    for sym in symbols:
        try:
            bars = load_bars(conn, sym, start, end, warmup_sessions=spec.warmup(sig_params) * 3)
        except Exception as exc:  # noqa: BLE001
            _read_failed(conn, f"raw_market.stock_daily[{sym}]", exc, errors)
            continue
        dates = [b["date"] for b in bars]
        closes = [b["close"] for b in bars]
        events.extend((sym, d) for d in signal_dates(dates, closes, spec.id, sig_params) if start <= d <= end)
    events.sort(key=lambda e: (e[1], e[0]))
    return ResolvedEvents(
        events=events,
        source="indicator" if not errors else "unavailable",
        errors=errors,
        notes=f"{spec.label} {sig_params} on adjusted daily closes; fires on the crossing session's close",
    )


def _resolve_pine_signal_events(
    conn: Any,
    params: Mapping[str, Any],
    start: date,
    end: date,
) -> ResolvedEvents:
    """Sessions a Pine library script fired, from ``features.stock_signal_pine_daily``."""
    script = str(params.get("script") or "").strip()
    side = str(params.get("side") or "buy").strip()
    if not script:
        raise ValueError("pine_signal needs params.script")
    if side not in ("buy", "sell"):
        raise ValueError("pine_signal side must be buy or sell")
    symbols = _params_symbols(params)
    where_sym = "AND symbol = ANY(%s::text[])" if symbols else ""
    args: list[Any] = [script, side, start, end]
    if symbols:
        args.append(symbols)
    events: list[tuple[str, date]] = []
    errors: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT UPPER(symbol), trade_date
                FROM features.stock_signal_pine_daily
                WHERE script_id = %s AND side = %s AND trade_date BETWEEN %s AND %s
                  {where_sym}
                ORDER BY trade_date, symbol
                """,
                tuple(args),
            )
            for sym, td in cur.fetchall() or []:
                events.append((str(sym), td.date() if isinstance(td, datetime) else td))
    except Exception as exc:  # noqa: BLE001
        _read_failed(conn, "features.stock_signal_pine_daily", exc, errors)
    return ResolvedEvents(
        events=events,
        source="pine" if not errors else "unavailable",
        errors=errors,
        notes=f"Pine script {script} {side} plot; fires on the session's close",
    )


def resolve_events(
    conn: Any,
    event_def: EventDef,
    lookback_years: int,
    *,
    today: date | None = None,
) -> ResolvedEvents:
    start, end = _lookback_window(lookback_years, today=today)
    return resolve_events_between(conn, event_def, start, end)


def resolve_events_between(conn: Any, event_def: EventDef, start: date, end: date) -> ResolvedEvents:
    """``resolve_events`` over an explicit window — the simulator's entry rule."""
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
    if kind == "indicator_signal":
        return _resolve_indicator_signal_events(conn, params, start, end)
    if kind == "pine_signal":
        return _resolve_pine_signal_events(conn, params, start, end)
    if kind == "sql":
        return _resolve_sql_events(conn, params, start, end)
    raise ValueError(f"unknown event kind {kind!r}")


# ---------------------------------------------------------------------------
# Price lookups
# ---------------------------------------------------------------------------


def _fetch_stock_price(conn: Any, symbol: str, on_or_before: date) -> dict[str, Any] | None:
    """Return {bar_date, open, close, close_as_traded} on or before ``on_or_before`` (latest).

    ``close`` is adjusted, so a stock leg's entry and exit sit on one scale;
    ``close_as_traded`` is the printed close, the scale an option leg's strikes
    were listed on (they differ after a later split or spin-off).
    """
    clause, sym_params = stock_clause(conn, symbol)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT bar_date, open, close, COALESCE(close_unadjusted, close) AS close_as_traded
                FROM raw_market.stock_daily
                WHERE {clause}
                  AND bar_date <= %s
                ORDER BY bar_date DESC
                LIMIT 1
                """,
                (*sym_params, on_or_before),
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
            "close_as_traded": row.get("close_as_traded", row.get("close")),
        }
    return {
        "bar_date": row[0],
        "open": row[1],
        "close": row[2],
        "close_as_traded": row[3] if len(row) > 3 else row[2],
    }


def _trading_sessions(conn: Any, symbol: str, start: date, end: date) -> list[date]:
    """The underlying's trading days in ``[start, end]``, ascending (across a rename)."""
    clause, sym_params = stock_clause(conn, symbol)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT bar_date FROM raw_market.stock_daily
                WHERE {clause}
                  AND bar_date BETWEEN %s AND %s
                ORDER BY bar_date
                """,
                (*sym_params, start, end),
            )
            rows = cur.fetchall() or []
    except Exception as exc:  # pragma: no cover
        logger.debug("session lookup failed for %s: %s", symbol, exc)
        return []
    out: list[date] = []
    for r in rows:
        d = r.get("bar_date") if isinstance(r, Mapping) else r[0]
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(d, date):
            out.append(d)
    return out


def _risk_free_rate(conn: Any, on_or_before: date, cache: dict[date, float] | None = None) -> float:
    """1-month Treasury yield on or before the date, as a decimal; 0.0 if unknown.

    Every Black–Scholes call in research ran at r = 0. At the ~4% short rates of
    2025–26 that moves a 45-DTE strike's implied delta by about 0.01–0.02 —
    enough to pick the neighbouring strike.
    """
    if cache is not None and on_or_before in cache:
        return cache[on_or_before]
    rate = 0.0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(yield_1_month, yield_3_month)
                FROM raw_market.treasury_yield
                WHERE yield_date <= %s
                  AND yield_date > %s
                ORDER BY yield_date DESC
                LIMIT 1
                """,
                (on_or_before, on_or_before - timedelta(days=14)),
            )
            row = cur.fetchone()
        val = (row.get("coalesce") if isinstance(row, Mapping) else row[0]) if row else None
        if val is not None:
            v = float(val)
            # The vendor reports percent (4.31); guard against a decimal feed.
            rate = v / 100.0 if v > 1.0 else v
    except Exception as exc:  # pragma: no cover
        logger.debug("treasury lookup failed for %s: %s", on_or_before, exc)
        rate = 0.0
    if not (0.0 <= rate < 0.25):
        rate = 0.0
    if cache is not None:
        cache[on_or_before] = rate
    return rate


# How far back a leg may reach for a contract's last bar when it did not trade on
# the session itself. Beyond this the price is too stale to stand for the day.
_STALE_LOOKBACK_DAYS = 7


def _bar_to_dict(r: Any) -> dict[str, Any]:
    if isinstance(r, Mapping):
        d = dict(r)
    else:
        d = {
            "option_ticker": r[0],
            "expiry": r[1],
            "strike": r[2],
            "bar_date": r[3],
            "open": r[4],
            "high": r[5],
            "low": r[6],
            "close": r[7],
        }
    for key in ("strike", "close", "open", "high", "low"):
        if d.get(key) is not None:
            d[key] = float(d[key])
    return d


def _pick_option(
    conn: Any,
    symbol: str,
    on_or_before: date,
    right: str,
    target_dte: int,
    strike_target: float,
    tolerance_pct: float = 0.30,
    *,
    target_delta: float | None = None,
    spot: float | None = None,
    rate: float = 0.0,
    expiry: date | None = None,
) -> dict[str, Any] | None:
    """Pick the contract to open on ``on_or_before`` and return its bar there.

    Reads the chain as listed over the last ``_STALE_LOOKBACK_DAYS`` up to the
    session, one bar per contract (its latest). Expiry: ``expiry`` when given
    (a wing follows its short leg), else the one closest to
    ``on_or_before + target_dte``. Strike: with ``target_delta`` (and ``spot``),
    the contract whose delta from its own implied vol is closest; otherwise the
    nearest strike to ``strike_target`` within ``tolerance_pct``. Contracts that
    traded on the session itself are preferred; a fallback bar is marked with
    ``stale_days``.

    Until 0.169.0 this took the latest 400 bars across every expiry and strike —
    a single session of a large name — so the target expiry was often not among
    them, and an untraded day silently returned a weeks-old bar. Adjusted
    contracts stay out: 5,346 of their bars share expiry, strike and right with
    a standard bar (2026-10-01), and nothing here would break the tie.
    """
    on = on_or_before
    if expiry is not None:
        exp_lo, exp_hi = expiry, expiry
    else:
        exp_lo = on + timedelta(days=1)
        exp_hi = on + timedelta(days=max(1, int(target_dte)) * 2 + 14)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT option_ticker, expiry, strike, bar_date, open, high, low, close
                FROM raw_market.option_daily
                WHERE underlying = %s
                  AND option_right = %s
                  AND bar_date BETWEEN %s AND %s
                  AND expiry BETWEEN %s AND %s
                  AND close > 0
                  AND {not_adjusted_contract_sql("option_ticker")}
                """,
                (
                    symbol.strip().upper(),
                    right,
                    on - timedelta(days=_STALE_LOOKBACK_DAYS),
                    on,
                    exp_lo,
                    exp_hi,
                ),
            )
            rows = cur.fetchall() or []
    except Exception as exc:  # pragma: no cover
        logger.debug("option lookup failed for %s@%s: %s", symbol, on, exc)
        return None
    if not rows:
        return None

    latest: dict[str, dict[str, Any]] = {}
    for raw in rows:
        bar = _bar_to_dict(raw)
        key = str(bar.get("option_ticker") or (bar.get("expiry"), bar.get("strike")))
        if bar.get("expiry") is None or bar.get("close") is None:
            continue
        prev = latest.get(key)
        if prev is None or bar["bar_date"] > prev["bar_date"]:
            latest[key] = bar
    candidates = list(latest.values())
    if not candidates:
        return None

    target_expiry = on + timedelta(days=max(1, int(target_dte)))
    expiries = sorted({c["expiry"] for c in candidates})
    best_expiry = expiry if expiry is not None else min(
        expiries, key=lambda e: (abs((e - target_expiry).days), e)
    )
    same_expiry = [c for c in candidates if c["expiry"] == best_expiry]
    if not same_expiry:
        return None
    fresh = [c for c in same_expiry if c["bar_date"] == on]
    pool_all = fresh or same_expiry

    chosen: dict[str, Any] | None = None
    if target_delta is not None and spot and spot > 0:
        # Late import keeps the module importable without the vol engine's deps.
        from bifrost_research.engines.backtest.canonical_pnl import bs_delta
        from bifrost_research.engines.volatility.iv_solver import solve_iv

        t_years = max((best_expiry - on).days, 1) / 365.0
        want = abs(float(target_delta))
        scored: list[tuple[float, dict[str, Any]]] = []
        for c in pool_all:
            r = "C" if right == "C" else "P"
            iv, status = solve_iv(spot, c["strike"], t_years, c["close"], r, rate=rate)
            if iv is None or status != "ok":
                continue
            delta = bs_delta(spot, c["strike"], t_years, iv, right=r, rate=rate)
            c = {**c, "iv": round(iv, 6), "delta": round(delta, 6)}
            scored.append((abs(abs(delta) - want), c))
        if scored:
            chosen = min(scored, key=lambda sc: (sc[0], sc[1]["strike"]))[1]
        else:
            return None
    else:
        band = abs(strike_target) * tolerance_pct
        within = [c for c in pool_all if abs(c["strike"] - strike_target) <= max(band, 0.01)]
        pool = within or pool_all
        chosen = min(pool, key=lambda c: (abs(c["strike"] - strike_target), c["strike"]))
    if chosen is None:
        return None
    chosen = dict(chosen)
    chosen["stale_days"] = (on - chosen["bar_date"]).days
    return chosen


def _fetch_contract_bar(conn: Any, option_ticker: str, on_or_before: date) -> dict[str, Any] | None:
    """The contract's own bar on (or within ``_STALE_LOOKBACK_DAYS`` before) a session.

    Exits read the contract the leg opened — never a re-pick — so a held leg
    cannot drift to another strike or expiry between entry and exit.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT option_ticker, expiry, strike, bar_date, open, high, low, close
                FROM raw_market.option_daily
                WHERE option_ticker = %s
                  AND bar_date BETWEEN %s AND %s
                  AND close > 0
                ORDER BY bar_date DESC
                LIMIT 1
                """,
                (option_ticker, on_or_before - timedelta(days=_STALE_LOOKBACK_DAYS), on_or_before),
            )
            row = cur.fetchone()
    except Exception as exc:  # pragma: no cover
        logger.debug("contract bar lookup failed for %s@%s: %s", option_ticker, on_or_before, exc)
        return None
    if row is None:
        return None
    bar = _bar_to_dict(row)
    bar["stale_days"] = (on_or_before - bar["bar_date"]).days
    return bar


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


def _iso(d: Any) -> str:
    return d.isoformat() if isinstance(d, date) else str(d)


def _price_option_leg(
    conn: Any,
    symbol: str,
    entry_date: date,
    exit_date: date,
    leg: LegSpec,
    *,
    fill_config: Any | None = None,
    anchor: LegPricing | None = None,
    rate_cache: dict[date, float] | None = None,
    delisted_on: date | None = None,
) -> LegPricing | None:
    stock_entry = _fetch_stock_price(conn, symbol, entry_date)
    if not stock_entry or stock_entry.get("close") is None:
        return None
    spot = float(stock_entry.get("close_as_traded") or stock_entry["close"])
    right = leg.option_right or "C"

    anchor_expiry: date | None = None
    if anchor is not None and anchor.strike is not None and anchor.expiry:
        # A wing: a fixed distance beyond its short strike, on the same expiry.
        strike_target = float(anchor.strike) + float(leg.anchor_offset_pct) * spot
        anchor_expiry = date.fromisoformat(str(anchor.expiry))
        target_delta = None
    else:
        strike_target = spot * (1.0 + float(leg.target_moneyness_offset))
        target_delta = leg.target_delta
    rate = _risk_free_rate(conn, entry_date, rate_cache) if target_delta is not None else 0.0

    pick_kwargs: dict[str, Any] = dict(
        symbol=symbol,
        on_or_before=entry_date,
        right=right,
        target_dte=leg.target_dte,
        strike_target=strike_target,
    )
    if target_delta is not None:
        pick_kwargs.update(target_delta=target_delta, spot=spot, rate=rate)
    if anchor_expiry is not None:
        pick_kwargs.update(expiry=anchor_expiry)
    entry_option = _pick_option(conn, **pick_kwargs)
    if not entry_option:
        return None
    if anchor is not None and anchor.strike is not None:
        # A wing that landed on (or inside) its short strike is no wing.
        beyond = (entry_option["strike"] - float(anchor.strike)) * (1 if leg.anchor_offset_pct > 0 else -1)
        if beyond <= 0:
            return None

    expiry = entry_option["expiry"]
    if isinstance(expiry, datetime):
        expiry = expiry.date()
    exit_side = "sell" if leg.side == "buy" else "buy"
    settled_at_expiry = exit_date >= expiry
    exit_pricing_source = "option_close"
    if settled_at_expiry:
        # Held to expiry: the contract is worth its intrinsic value against the
        # underlying's close that day. Until 0.169.0 the exit re-picked with
        # ``expiry > exit_date``, which excluded this very contract and priced
        # the exit on the next expiry's — a month of time value still in it.
        stock_exp = _fetch_stock_price(conn, symbol, expiry)
        if not stock_exp or stock_exp.get("close") is None:
            return None
        spot_exp = float(stock_exp.get("close_as_traded") or stock_exp["close"])
        strike = float(entry_option["strike"])
        exit_price = max(0.0, spot_exp - strike) if right == "C" else max(0.0, strike - spot_exp)
        exit_bar_date: Any = stock_exp.get("bar_date")
        exit_stale = 0
        exit_leg_date = expiry
    else:
        exit_option = _fetch_contract_bar(conn, str(entry_option.get("option_ticker")), exit_date)
        exit_pricing_source = "option_close"
        if not exit_option and delisted_on is not None and exit_date == delisted_on:
            # The underlying stopped trading and the contract left no print to
            # close at: it is worth its intrinsic value against the last close.
            stock_last = _fetch_stock_price(conn, symbol, delisted_on)
            if not stock_last or stock_last.get("close") is None:
                return None
            spot_last = float(stock_last.get("close_as_traded") or stock_last["close"])
            strike = float(entry_option["strike"])
            intrinsic = max(0.0, spot_last - strike) if right == "C" else max(0.0, strike - spot_last)
            exit_option = {"close": intrinsic, "bar_date": stock_last.get("bar_date"), "stale_days": 0}
            exit_pricing_source = "delisting_intrinsic"
        if not exit_option:
            return None
        exit_price = _apply_fill(leg, exit_option, side=exit_side, fill_config=fill_config)
        exit_bar_date = exit_option.get("bar_date")
        exit_stale = int(exit_option.get("stale_days") or 0)
        exit_leg_date = exit_date

    entry_price = _apply_fill(leg, entry_option, side=leg.side, fill_config=fill_config)

    sign = leg_signs(leg)
    contract_mult = int(getattr(fill_config, "multiplier", 100) if fill_config is not None else 100)
    gross = sign * (exit_price - entry_price) * leg.quantity * contract_mult
    commission = 0.0
    if fill_config is not None:
        sides = 1 if settled_at_expiry else 2
        commission = float(getattr(fill_config, "commission_per_contract", 0.0)) * leg.quantity * sides
    pnl = gross - commission
    fill_details: dict[str, Any] = {
        "pricing_source": "option_close",
        "exit_pricing_source": "expiry_intrinsic" if settled_at_expiry else exit_pricing_source,
        "option_ticker": entry_option.get("option_ticker"),
        "spot": round(spot, 6),
        "strike_target": round(strike_target, 6),
        "entry_bar": _iso(entry_option.get("bar_date")),
        "exit_bar": _iso(exit_bar_date),
        "entry_stale_days": int(entry_option.get("stale_days") or 0),
        "exit_stale_days": exit_stale,
        "commission": round(commission, 6),
    }
    if target_delta is not None:
        fill_details.update(
            target_delta=float(target_delta),
            entry_iv=entry_option.get("iv"),
            entry_delta=entry_option.get("delta"),
            rate=round(rate, 6),
        )
    return LegPricing(
        label=leg.label or "option",
        kind="option",
        side=leg.side,
        quantity=leg.quantity,
        entry_date=entry_date.isoformat(),
        exit_date=exit_leg_date.isoformat(),
        entry_price=round(entry_price, 6),
        exit_price=round(exit_price, 6),
        strike=float(entry_option["strike"]),
        expiry=_iso(expiry),
        option_right=right,
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
    clause, sym_params = stock_clause(conn, symbol)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT bar_date, high, low, close
                FROM raw_market.stock_daily
                WHERE {clause}
                  AND bar_date BETWEEN %s AND %s
                ORDER BY bar_date
                """,
                (*sym_params, entry_date, exit_date),
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


def _clip_to_listing_end(
    leg: LegSpec, event_date: date, sessions: Sequence[date], *, after_event: bool = False
) -> tuple[date, date] | None:
    """The leg's window with its exit pulled back to the last session.

    Only for a delisted underlying (B7): there is no later price, so the last
    close is the exit. The entry still has to fall inside the listing's life.
    ``after_event`` anchors on the session after the event, as in
    ``resolve_trading_window``.
    """
    days = sorted(set(sessions))
    anchor = next((i for i, d in enumerate(days) if (d > event_date if after_event else d >= event_date)), None)
    if anchor is None:
        return None
    lo, hi = sorted((int(leg.entry_offset_days), int(leg.exit_offset_days)))
    i_entry, i_exit = anchor + lo, min(anchor + hi, len(days) - 1)
    if i_entry < 0 or i_entry >= len(days) or i_exit < i_entry:
        return None
    return days[i_entry], days[i_exit]


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
        # A signal is computed from its session's close: offsets count from the
        # next session, so offset 0 cannot fill on the close that made it (0.175.0).
        after_event = entry_after_event(event_def.kind)

        runs: list[EventRun] = []
        skipped = 0
        skipped_no_option = 0
        skipped_no_stock = 0
        skipped_incomplete_window = 0
        rate_cache: dict[date, float] = {}
        # Calendar span that surely covers the session offsets (~1.5 calendar
        # days per session, plus a holiday week either side).
        lo_off = min(min(int(lg.entry_offset_days), int(lg.exit_offset_days)) for lg in legs)
        hi_off = max(max(int(lg.entry_offset_days), int(lg.exit_offset_days)) for lg in legs)
        pad_before = int(abs(min(lo_off, 0)) * 1.5) + 10
        pad_after = int((max(hi_off, 0) + (1 if after_event else 0)) * 1.5) + 10
        as_of = today or date.today()
        delisted_exits = 0
        for raw_symbol, event_date in resolved.events[: max(1, int(max_events))]:
            # Options sit under the ticker the company trades as now (Plugin
            # 0.51.0); the stock reads splice the old ticker's bars in (B7).
            symbol = live_label(raw_symbol)
            sessions = _trading_sessions(
                conn,
                symbol,
                event_date - timedelta(days=pad_before),
                event_date + timedelta(days=pad_after),
            )
            if not sessions:
                skipped += 1
                skipped_no_stock += 1
                continue
            leg_pricings: list[LegPricing] = []
            entry_dates: list[date] = []
            exit_dates: list[date] = []
            skip_run = False
            delisted_on: date | None = None
            ended_checked = False
            for leg in legs:
                window = resolve_trading_window(leg, event_date, sessions, after_event=after_event)
                if window is None and not ended_checked:
                    ended_checked = True
                    end = listing_end(conn, symbol, as_of=as_of)
                    if end is not None and sessions and sessions[-1] == end:
                        delisted_on = end
                if window is None and delisted_on is not None:
                    window = _clip_to_listing_end(leg, event_date, sessions, after_event=after_event)
                if window is None:
                    # The exit has not happened yet (or the history starts after
                    # the entry): there is nothing honest to price it at.
                    skipped_incomplete_window += 1
                    skip_run = True
                    break
                entry_date, exit_date = window
                entry_dates.append(entry_date)
                exit_dates.append(exit_date)
                if leg.kind == "stock":
                    pricing = _price_stock_leg(conn, symbol, entry_date, exit_date, leg)
                else:
                    anchor = leg_pricings[leg.anchor_leg] if leg.anchor_leg is not None else None
                    pricing = _price_option_leg(
                        conn,
                        symbol,
                        entry_date,
                        exit_date,
                        leg,
                        fill_config=fill_config,
                        anchor=anchor,
                        rate_cache=rate_cache,
                        delisted_on=delisted_on,
                    )
                if pricing is None:
                    # Which data was missing decides whether an empty result means
                    # "no edge" or "no history". Reporting only the count lets the
                    # UI render the second as the first.
                    if leg.kind == "stock":
                        skipped_no_stock += 1
                    else:
                        skipped_no_option += 1
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
            notes = "D10 BLOCKED — historical replay only"
            if delisted_on is not None and exit_ts == delisted_on:
                delisted_exits += 1
                notes = f"delisted: closed at the last close {delisted_on.isoformat()}; " + notes
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
                    notes=notes,
                )
            )
    finally:
        if close_conn:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass

    summary = _summarize(runs)
    summary["skipped_events"] = skipped
    summary["skipped_no_option"] = skipped_no_option
    summary["skipped_no_stock"] = skipped_no_stock
    summary["skipped_incomplete_window"] = skipped_incomplete_window
    summary["delisted_exits"] = delisted_exits
    summary["offset_unit"] = "trading_sessions"
    summary["entry_timing"] = entry_timing(event_def.kind, "close")
    summary["event_source"] = resolved.source
    summary["event_source_errors"] = list(resolved.errors)
    summary["evaluation"] = evaluation("event_backtest")
    return {
        "runs": [_run_to_dict(r) for r in runs],
        "summary": summary,
        "event_source": resolved.source,
        "event_source_notes": resolved.notes,
        "event_source_errors": list(resolved.errors),
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
    "resolve_events_between",
    "run_event_query",
    "summarize_runs",
]
