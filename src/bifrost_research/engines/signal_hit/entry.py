"""Cron entrypoint — lens trigger hit table builder (Analyze Wave I, A3 lens expansion).

Sources (the lens list is the registry's decay lenses):
- IV Rank: features.option_metric_iv_percentile_daily.iv_rank_1y (0-100)
- VRP: features.stock_signal_vrp_daily.vrp_pct_252d (0-100)
- OpEx Pin: max_pain vs stock_daily close → pin_pct_distance
- Skew: features.option_surface_fit_daily.atm_slope at the ~30 DTE expiry
- GEX regime: features.option_metric_gex_levels_daily.total_net_gex at the ~30 DTE expiry
- Terrain regime: features.stock_forecast_terrain_daily.regime (crash-risk only)
- Order sentiment: features.option_flow_sentiment_daily, tape-sourced rows only

Forward returns from raw_market.stock_daily (T+5 / T+20 sessions); the hit rule
per lens (mean-revert / follow / magnitude) comes from the registry.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from bifrost_research.db.conn import connect
from bifrost_research.db.upsert import batch_upsert
from bifrost_research.engines.signal_hit.build import (
    classify_gex_regime,
    classify_iv_rank,
    classify_opex_pin,
    classify_momentum,
    classify_order_sentiment,
    classify_sepa,
    classify_skew,
    classify_terrain_regime,
    classify_vrp,
    hit_for,
)
from bifrost_research.lenses.registry import decay_lens_ids
from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_LENS_HIT_DAILY

logger = logging.getLogger(__name__)
_NY = ZoneInfo("America/New_York")

LENS_IV = "iv_rank"
LENS_VRP = "vrp"
LENS_OPEX = "opex_pin"
LENS_SKEW = "skew"
LENS_GEX = "gex_regime"
LENS_TERRAIN = "terrain_regime"
LENS_SENTIMENT = "order_sentiment"
LENS_SEPA = "sepa"
LENS_MOMENTUM = "momentum"
ALL_LENSES = decay_lens_ids()

UPSERT_COLS = (
    "trade_date",
    "symbol",
    "lens",
    "trigger_side",
    "trigger_value",
    "fwd_return_5d",
    "fwd_return_20d",
    "hit_5d",
    "hit_20d",
    "computed_at",
)


def _today_ny() -> date:
    return datetime.now(timezone.utc).astimezone(_NY).date()


def _parse_lenses(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return list(ALL_LENSES)
    out: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in ALL_LENSES:
            raise ValueError(f"unknown lens: {name}")
        out.append(name)
    return out or list(ALL_LENSES)


def _trading_days(conn: Any, start: date, end: date) -> list[date]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT bar_date
            FROM raw_market.stock_daily
            WHERE bar_date >= %s AND bar_date <= %s
            ORDER BY bar_date ASC
            """,
            (start, end),
        )
        rows = cur.fetchall() or []
    days: list[date] = []
    for row in rows:
        d = row[0] if not isinstance(row, dict) else row.get("bar_date")
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(d, date):
            days.append(d)
    return days


def _fwd_return(conn: Any, symbol: str, as_of: date, horizon: int) -> float | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bar_date, close::float
            FROM raw_market.stock_daily
            WHERE symbol = %s
              AND bar_date >= %s
              AND close IS NOT NULL AND close > 0
            ORDER BY bar_date ASC
            LIMIT %s
            """,
            (symbol.upper(), as_of, horizon + 1),
        )
        rows = cur.fetchall() or []
    if len(rows) < horizon + 1:
        return None
    c0 = float(rows[0][1])
    c1 = float(rows[horizon][1])
    if c0 <= 0:
        return None
    return (c1 / c0) - 1.0


def _load_iv_triggers(conn: Any, trade_date: date) -> list[tuple[str, str, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, iv_rank_1y::float
            FROM features.option_metric_iv_percentile_daily
            WHERE trade_date = %s AND iv_rank_1y IS NOT NULL
            """,
            (trade_date,),
        )
        rows = cur.fetchall() or []
    out: list[tuple[str, str, float]] = []
    for row in rows:
        sym, val = row[0], float(row[1])
        side = classify_iv_rank(val)
        if side:
            out.append((str(sym).upper(), side, val))
    return out


def _load_vrp_triggers(conn: Any, trade_date: date) -> list[tuple[str, str, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, vrp_pct_252d::float
            FROM features.stock_signal_vrp_daily
            WHERE trade_date = %s AND vrp_pct_252d IS NOT NULL
            """,
            (trade_date,),
        )
        rows = cur.fetchall() or []
    out: list[tuple[str, str, float]] = []
    for row in rows:
        sym, val = row[0], float(row[1])
        side = classify_vrp(val)
        if side:
            out.append((str(sym).upper(), side, val))
    return out


def _load_opex_triggers(conn: Any, trade_date: date) -> list[tuple[str, str, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH nearest_mp AS (
                SELECT DISTINCT ON (m.symbol)
                    m.symbol,
                    m.max_pain_strike
                FROM features.option_metric_max_pain_daily m
                WHERE m.trade_date = %s
                  AND m.max_pain_strike IS NOT NULL
                  AND m.max_pain_strike > 0
                ORDER BY m.symbol, ABS((m.expiry - m.trade_date) - 30) ASC, m.expiry ASC
            )
            SELECT n.symbol,
                   (s.close::float - n.max_pain_strike) / NULLIF(s.close::float, 0) AS pin_pct
            FROM nearest_mp n
            JOIN raw_market.stock_daily s
              ON s.symbol = n.symbol AND s.bar_date = %s
            WHERE s.close IS NOT NULL AND s.close > 0
            """,
            (trade_date, trade_date),
        )
        rows = cur.fetchall() or []
    out: list[tuple[str, str, float]] = []
    for row in rows:
        sym, pin = row[0], float(row[1])
        side = classify_opex_pin(pin)
        if side:
            out.append((str(sym).upper(), side, pin))
    return out


def _load_sepa_triggers(conn: Any, trade_date: date) -> list[tuple[str, str, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, sepa_score::float, path
            FROM features.stock_signal_sepa_daily
            WHERE trade_date = %s AND sepa_score IS NOT NULL
            """,
            (trade_date,),
        )
        rows = cur.fetchall() or []
    out: list[tuple[str, str, float]] = []
    for sym, score, path in rows:
        side = classify_sepa(score, path)
        if side:
            out.append((str(sym).upper(), side, float(score)))
    return out


def _load_momentum_triggers(conn: Any, trade_date: date) -> list[tuple[str, str, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, score::float, grade
            FROM features.stock_signal_momentum_daily
            WHERE trade_date = %s AND score IS NOT NULL
            """,
            (trade_date,),
        )
        rows = cur.fetchall() or []
    out: list[tuple[str, str, float]] = []
    for sym, score, grade in rows:
        side = classify_momentum(score, grade)
        if side:
            out.append((str(sym).upper(), side, float(score)))
    return out


def _load_skew_triggers(conn: Any, trade_date: date) -> list[tuple[str, str, float]]:
    # C2: today's near-30-DTE slope against the symbol's own prior 252 days —
    # the percentile is the share of those days whose |slope| sat below today's.
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH today AS (
                SELECT DISTINCT ON (symbol) symbol, atm_slope::float AS slope
                FROM features.option_surface_fit_daily
                WHERE trade_date = %s AND atm_slope IS NOT NULL
                ORDER BY symbol, ABS(dte - 30) ASC, expiry ASC
            ),
            hist AS (
                SELECT DISTINCT ON (symbol, trade_date) symbol, trade_date, ABS(atm_slope)::float AS a
                FROM features.option_surface_fit_daily
                WHERE atm_slope IS NOT NULL
                  AND trade_date < %s AND trade_date >= %s::date - INTERVAL '252 days'
                ORDER BY symbol, trade_date, ABS(dte - 30) ASC, expiry ASC
            )
            SELECT t.symbol, t.slope,
                   100.0 * COUNT(h.a) FILTER (WHERE h.a < ABS(t.slope)) / NULLIF(COUNT(h.a), 0),
                   COUNT(h.a)
            FROM today t
            LEFT JOIN hist h ON h.symbol = t.symbol
            GROUP BY t.symbol, t.slope
            """,
            (trade_date, trade_date, trade_date),
        )
        rows = cur.fetchall() or []
    out: list[tuple[str, str, float]] = []
    for row in rows:
        sym, slope = row[0], float(row[1])
        pctile = float(row[2]) if row[2] is not None else None
        side = classify_skew(slope, pctile, int(row[3] or 0))
        if side:
            out.append((str(sym).upper(), side, slope))
    return out


def _load_gex_triggers(conn: Any, trade_date: date) -> list[tuple[str, str, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (symbol) symbol, total_net_gex::float
            FROM features.option_metric_gex_levels_daily
            WHERE trade_date = %s AND total_net_gex IS NOT NULL
            ORDER BY symbol, ABS((expiry - trade_date) - 30) ASC, expiry ASC
            """,
            (trade_date,),
        )
        rows = cur.fetchall() or []
    out: list[tuple[str, str, float]] = []
    for row in rows:
        sym, net = row[0], float(row[1])
        side = classify_gex_regime(net)
        if side:
            out.append((str(sym).upper(), side, net))
    return out


def _load_terrain_triggers(conn: Any, trade_date: date) -> list[tuple[str, str, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, regime, tail_risk::float
            FROM features.stock_forecast_terrain_daily
            WHERE trade_date = %s AND regime IS NOT NULL
            """,
            (trade_date,),
        )
        rows = cur.fetchall() or []
    out: list[tuple[str, str, float]] = []
    for row in rows:
        sym, regime, tail = row[0], row[1], row[2]
        side = classify_terrain_regime(regime)
        if side:
            out.append((str(sym).upper(), side, float(tail) if tail is not None else 0.0))
    return out


def _load_sentiment_triggers(conn: Any, trade_date: date) -> list[tuple[str, str, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, sentiment_score::float, data_source
            FROM features.option_flow_sentiment_daily
            WHERE trade_date = %s AND sentiment_score IS NOT NULL
            """,
            (trade_date,),
        )
        rows = cur.fetchall() or []
    out: list[tuple[str, str, float]] = []
    for row in rows:
        sym, score, source = row[0], float(row[1]), row[2]
        side = classify_order_sentiment(score, source)
        if side:
            out.append((str(sym).upper(), side, score))
    return out


def _watchlist() -> list[str]:
    raw = os.environ.get("RESEARCH_WATCHLIST", "")
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _filter_symbols(
    triggers: Iterable[tuple[str, str, float]],
    allowed: Sequence[str] | None,
) -> list[tuple[str, str, float]]:
    if not allowed:
        return list(triggers)
    allow = {s.upper() for s in allowed}
    return [t for t in triggers if t[0] in allow]


def build_rows_for_day(
    conn: Any,
    trade_date: date,
    lenses: Sequence[str],
    *,
    symbols: Sequence[str] | None = None,
) -> list[tuple[Any, ...]]:
    now = datetime.now(timezone.utc)
    loaders = {
        LENS_IV: _load_iv_triggers,
        LENS_VRP: _load_vrp_triggers,
        LENS_OPEX: _load_opex_triggers,
        LENS_SKEW: _load_skew_triggers,
        LENS_GEX: _load_gex_triggers,
        LENS_TERRAIN: _load_terrain_triggers,
        LENS_SENTIMENT: _load_sentiment_triggers,
        LENS_SEPA: _load_sepa_triggers,
        LENS_MOMENTUM: _load_momentum_triggers,
    }
    rows: list[tuple[Any, ...]] = []
    for lens in lenses:
        triggers = _filter_symbols(loaders[lens](conn, trade_date), symbols)
        for symbol, side, value in triggers:
            fwd5 = _fwd_return(conn, symbol, trade_date, 5)
            fwd20 = _fwd_return(conn, symbol, trade_date, 20)
            hit5 = hit_for(lens, side=side, fwd_return=fwd5, horizon=5)
            hit20 = hit_for(lens, side=side, fwd_return=fwd20, horizon=20)
            rows.append(
                (
                    trade_date,
                    symbol,
                    lens,
                    side,
                    value,
                    fwd5,
                    fwd20,
                    hit5,
                    hit20,
                    now,
                )
            )
    return rows


def backfill_missing_forward(
    conn: Any,
    *,
    horizons: Sequence[int] = (5, 20),
    max_rows: int = 20000,
) -> dict[str, Any]:
    """Fill forward columns on rows whose window has since elapsed.

    Re-walking a day and rebuilding its rows cannot repair these. A row is
    keyed by the lens trigger that fired on that date, and a rebuild only
    emits rows for triggers that fire on *today's* view of that date — a lens
    whose inputs have since moved no longer produces the row, so the upsert
    never reaches it and the NULL stands forever. On 2026-09-08 nineteen rows
    from 2026-08-03..08-07 sat unjudged for exactly that reason while the same
    run rewrote 198 of their neighbours.

    So repair is driven by what is missing, not by re-deriving what should
    exist: find the incomplete rows, recompute only their forward columns, and
    leave the trigger that identifies them untouched. A window that still has
    not elapsed stays NULL — an unknown outcome must never be recorded as a
    miss.
    """
    filled = {int(h): 0 for h in horizons}
    examined = 0
    # Horizons are ints from the caller, never user input. Carrying one flag per
    # horizon keeps the recompute to the columns that are actually blank: a row
    # selected for a missing 20d must not have its good 5d rewritten, or the
    # count reports eight hundred repairs where eleven happened.
    hs = [int(h) for h in horizons]
    missing_any = " OR ".join(f"fwd_return_{h}d IS NULL" for h in hs)
    flags = ", ".join(f"fwd_return_{h}d IS NULL" for h in hs)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT trade_date, symbol, lens, trigger_side, {flags}
            FROM {TABLE_STOCK_SIGNAL_LENS_HIT_DAILY}
            WHERE {missing_any}
            ORDER BY trade_date DESC
            LIMIT %s
            """,
            (max_rows,),
        )
        pending = cur.fetchall() or []

    for trade_date, symbol, lens, side, *blank in pending:
        examined += 1
        updates: list[tuple[str, Any]] = []
        for h, is_blank in zip(hs, blank):
            if not is_blank:
                continue
            fwd = _fwd_return(conn, str(symbol), trade_date, int(h))
            if fwd is None:
                continue  # window still open; leave it unknown
            hit = hit_for(str(lens), side=str(side), fwd_return=fwd, horizon=h)
            updates.append((f"fwd_return_{h}d", fwd))
            updates.append((f"hit_{h}d", hit))
            filled[h] += 1
        if not updates:
            continue
        sets = ", ".join(f"{c} = %s" for c, _ in updates)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {TABLE_STOCK_SIGNAL_LENS_HIT_DAILY}
                SET {sets}, computed_at = %s
                WHERE trade_date = %s AND symbol = %s AND lens = %s AND trigger_side = %s
                """,
                [v for _, v in updates]
                + [datetime.now(timezone.utc), trade_date, symbol, lens, side],
            )
        conn.commit()

    return {
        "mode": "backfill_missing_forward",
        "examined": examined,
        "filled": {f"{h}d": n for h, n in filled.items()},
    }


def run(
    *,
    lookback_days: int = 3,
    lenses: Sequence[str] | None = None,
    as_of: date | None = None,
    repair: bool = False,
) -> dict[str, Any]:
    lens_list = list(lenses) if lenses else list(ALL_LENSES)
    end = as_of or _today_ny()
    start = end - timedelta(days=max(lookback_days * 2, lookback_days + 5))
    conn = connect()
    try:
        days = _trading_days(conn, start, end)
        # Keep last N trading days within lookback calendar window
        days = [d for d in days if d >= end - timedelta(days=lookback_days + 10)][-lookback_days:]
        if not days:
            # fall back to calendar walk if stock_daily sparse
            days = [end - timedelta(days=i) for i in range(lookback_days)][::-1]

        watch = _watchlist()
        written = 0
        per_day: list[dict[str, Any]] = []
        for day in days:
            rows = build_rows_for_day(conn, day, lens_list, symbols=watch or None)
            if rows:
                batch_upsert(
                    conn,
                    TABLE_STOCK_SIGNAL_LENS_HIT_DAILY,
                    UPSERT_COLS,
                    rows,
                    conflict_keys=("trade_date", "symbol", "lens", "trigger_side"),
                    update_cols=(
                        "trigger_value",
                        "fwd_return_5d",
                        "fwd_return_20d",
                        "hit_5d",
                        "hit_20d",
                        "computed_at",
                    ),
                    set_fetched_at=False,
                )
            written += len(rows)
            per_day.append({"trade_date": day.isoformat(), "rows_written": len(rows)})
        repair_stats = backfill_missing_forward(conn) if repair else None
        return {
            "repair": repair_stats,
            "mode": "batch",
            "lookback_days": lookback_days,
            "lenses": lens_list,
            "days": per_day,
            "rows_written": written,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _clear_lens_hit(conn: Any, lenses: Sequence[str] | None = None) -> int:
    """Delete rows before full rebuild (Wave J --clear)."""
    with conn.cursor() as cur:
        if lenses:
            cur.execute(
                f"DELETE FROM {TABLE_STOCK_SIGNAL_LENS_HIT_DAILY} WHERE lens = ANY(%s)",
                (list(lenses),),
            )
        else:
            cur.execute(f"DELETE FROM {TABLE_STOCK_SIGNAL_LENS_HIT_DAILY}")
        deleted = cur.rowcount
    conn.commit()
    return int(deleted or 0)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Build features.stock_signal_lens_hit_daily")
    parser.add_argument("--lookback-days", type=int, default=3)
    parser.add_argument("--lens", type=str, default=",".join(ALL_LENSES))
    parser.add_argument("--as-of", type=str, default=None)
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete existing lens_hit rows for selected lenses before rebuild",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    lenses = _parse_lenses(args.lens)
    if args.clear:
        conn = connect()
        try:
            deleted = _clear_lens_hit(conn, lenses)
            logger.info("cleared %s lens_hit rows for lenses=%s", deleted, lenses)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    result = run(lookback_days=args.lookback_days, lenses=lenses, as_of=as_of)
    logger.info("signal_hit result=%s", result)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
