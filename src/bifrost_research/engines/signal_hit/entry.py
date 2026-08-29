"""Cron entrypoint — lens trigger hit table builder (Analyze Wave I).

Sources:
- IV Rank: features.option_metric_iv_percentile_daily.iv_rank_1y (0-100)
- VRP: features.stock_signal_vrp_daily.vrp_pct_252d (0-100)
- OpEx Pin: max_pain vs stock_daily close → pin_pct_distance

Forward returns from raw_market.stock_daily (T+5 / T+20 sessions).
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
    classify_iv_rank,
    classify_opex_pin,
    classify_vrp,
    side_aware_hit,
)
from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_LENS_HIT_DAILY

logger = logging.getLogger(__name__)
_NY = ZoneInfo("America/New_York")

LENS_IV = "iv_rank"
LENS_VRP = "vrp"
LENS_OPEX = "opex_pin"
ALL_LENSES = (LENS_IV, LENS_VRP, LENS_OPEX)

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
    }
    rows: list[tuple[Any, ...]] = []
    for lens in lenses:
        triggers = _filter_symbols(loaders[lens](conn, trade_date), symbols)
        for symbol, side, value in triggers:
            fwd5 = _fwd_return(conn, symbol, trade_date, 5)
            fwd20 = _fwd_return(conn, symbol, trade_date, 20)
            hit5 = side_aware_hit(side=side, fwd_return=fwd5)
            hit20 = side_aware_hit(side=side, fwd_return=fwd20)
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


def run(
    *,
    lookback_days: int = 3,
    lenses: Sequence[str] | None = None,
    as_of: date | None = None,
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
        return {
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
