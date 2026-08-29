"""Materialized scanner CronJob entrypoint — Analyze Wave D.

Usage::

    python -m bifrost_research.engines.scan.entry
    python -m bifrost_research.engines.scan.entry --symbol NVDA --dry-run
    python -m bifrost_research.engines.scan.entry --as-of 2026-08-22
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from bifrost_research.db.calendar import (
    fetch_recent_trading_days,
    load_symbols_from_env_or_query,
    union_iv_radar_benchmarks,
)
from bifrost_research.db.conn import connect
from bifrost_research.db.upsert import batch_upsert
from bifrost_research.engines.scan.build import build_scan_row
from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_SCAN_DAILY

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")

_SCAN_COLS = (
    "trade_date",
    "symbol",
    "close",
    "iv_rank_1y",
    "vrp_pct_252d",
    "atm_slope_30d",
    "pin_pct_distance",
    "dte_to_opex",
    "zero_gamma_offset",
    "gex_notional",
    "terrain_regime",
    "pin_score",
    "tail_risk",
    "trend_release",
    "composite_score",
    "lens_flags",
    "computed_at",
)

_AGGREGATE_SQL = """
WITH watchlist AS (
    SELECT UPPER(unnest(%s::text[])) AS symbol
),
day_symbols AS (
    SELECT symbol FROM features.option_metric_iv_percentile_daily WHERE trade_date = %s
    UNION SELECT symbol FROM features.stock_signal_vrp_daily WHERE trade_date = %s
    UNION SELECT symbol FROM features.option_surface_fit_daily WHERE trade_date = %s
    UNION SELECT symbol FROM features.option_metric_max_pain_daily WHERE trade_date = %s
    UNION SELECT symbol FROM features.option_metric_vanna_charm_daily WHERE trade_date = %s
    UNION SELECT symbol FROM features.option_metric_gex_levels_daily WHERE trade_date = %s
    UNION SELECT symbol FROM features.stock_forecast_terrain_daily WHERE trade_date = %s
    UNION SELECT symbol FROM watchlist
),
universe AS (
    SELECT DISTINCT UPPER(symbol) AS symbol
    FROM day_symbols
    WHERE symbol IS NOT NULL AND symbol <> ''
),
surface_30d AS (
    SELECT DISTINCT ON (symbol)
        symbol,
        atm_slope
    FROM features.option_surface_fit_daily
    WHERE trade_date = %s
      AND atm_slope IS NOT NULL
    ORDER BY symbol, ABS(dte - 30) ASC, expiry ASC
),
nearest_mp AS (
    SELECT DISTINCT ON (m.symbol)
        m.symbol,
        m.max_pain_strike
    FROM features.option_metric_max_pain_daily m
    WHERE m.trade_date = %s
      AND m.max_pain_strike IS NOT NULL
      AND m.max_pain_strike > 0
    ORDER BY m.symbol, ABS((m.expiry - m.trade_date) - 30) ASC, m.expiry ASC
),
pin AS (
    SELECT n.symbol,
           s.close::float AS close,
           (s.close::float - n.max_pain_strike)
               / NULLIF(s.close::float, 0) AS pin_pct_distance
    FROM nearest_mp n
    LEFT JOIN raw_market.stock_daily s
      ON s.symbol = n.symbol
     AND s.bar_date = %s
    WHERE s.close IS NOT NULL
      AND s.close > 0
),
gex_30d AS (
    SELECT DISTINCT ON (symbol)
        symbol,
        total_net_gex,
        (spot - zero_gamma) / NULLIF(spot, 0) AS zero_gamma_offset
    FROM features.option_metric_gex_levels_daily
    WHERE trade_date = %s
      AND spot IS NOT NULL
      AND spot > 0
    ORDER BY symbol, ABS((expiry - trade_date) - 30) ASC, expiry ASC
)
SELECT
    u.symbol,
    COALESCE(pin.close, tr.spot)::float AS close,
    iv.iv_rank_1y::float,
    vrp.vrp_pct_252d::float,
    sf.atm_slope::float AS atm_slope_30d,
    pin.pin_pct_distance::float,
    vc.dte_to_opex,
    gex.zero_gamma_offset::float,
    gex.total_net_gex::float AS gex_notional,
    tr.regime AS terrain_regime,
    tr.pin_score::float,
    tr.tail_risk::float,
    tr.trend_release::float
FROM universe u
LEFT JOIN (
    SELECT symbol, iv_rank_1y
    FROM features.option_metric_iv_percentile_daily
    WHERE trade_date = %s
) iv ON iv.symbol = u.symbol
LEFT JOIN (
    SELECT symbol, vrp_pct_252d
    FROM features.stock_signal_vrp_daily
    WHERE trade_date = %s
) vrp ON vrp.symbol = u.symbol
LEFT JOIN surface_30d sf ON sf.symbol = u.symbol
LEFT JOIN pin ON pin.symbol = u.symbol
LEFT JOIN (
    SELECT symbol, dte_to_opex
    FROM features.option_metric_vanna_charm_daily
    WHERE trade_date = %s
) vc ON vc.symbol = u.symbol
LEFT JOIN gex_30d gex ON gex.symbol = u.symbol
LEFT JOIN (
    SELECT symbol, regime, pin_score, tail_risk, trend_release, spot
    FROM features.stock_forecast_terrain_daily
    WHERE trade_date = %s
) tr ON tr.symbol = u.symbol
ORDER BY u.symbol
"""


def _today_ny() -> date:
    return datetime.now(timezone.utc).astimezone(_NY).date()


def _row_to_dict(row: Any, columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {columns[i]: row[i] for i in range(min(len(columns), len(row)))}


def fetch_scan_source_rows(
    conn: Any,
    trade_date: date,
    watchlist: Sequence[str],
    *,
    symbols_filter: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    watch = [s.strip().upper() for s in watchlist if s and s.strip()]
    params: list[Any] = [
        watch,
        trade_date,
        trade_date,
        trade_date,
        trade_date,
        trade_date,
        trade_date,
        trade_date,
        trade_date,
        trade_date,
        trade_date,
        trade_date,
        trade_date,
        trade_date,
        trade_date,
        trade_date,
    ]
    sql = _AGGREGATE_SQL
    if symbols_filter:
        sql += "\nWHERE u.symbol = ANY(%s)\n"
        params.append([s.strip().upper() for s in symbols_filter])
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [_row_to_dict(r, cols) for r in cur.fetchall()]


def compute_scan_for_date(
    conn: Any,
    *,
    trade_date: date,
    watchlist: Sequence[str],
    symbols_filter: Sequence[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    raw_rows = fetch_scan_source_rows(
        conn,
        trade_date,
        watchlist,
        symbols_filter=symbols_filter,
    )
    computed_at = datetime.now(timezone.utc)
    built = [
        build_scan_row(
            trade_date=trade_date,
            symbol=str(row["symbol"]),
            close=row.get("close"),
            iv_rank_1y=row.get("iv_rank_1y"),
            vrp_pct_252d=row.get("vrp_pct_252d"),
            atm_slope_30d=row.get("atm_slope_30d"),
            pin_pct_distance=row.get("pin_pct_distance"),
            dte_to_opex=row.get("dte_to_opex"),
            zero_gamma_offset=row.get("zero_gamma_offset"),
            gex_notional=row.get("gex_notional"),
            terrain_regime=row.get("terrain_regime"),
            pin_score=row.get("pin_score"),
            tail_risk=row.get("tail_risk"),
            trend_release=row.get("trend_release"),
            computed_at=computed_at,
        )
        for row in raw_rows
    ]
    if dry_run:
        return {
            "trade_date": trade_date.isoformat(),
            "universe_size": len(built),
            "preview": built[:5],
            "dry_run": True,
        }
    if not built:
        return {
            "trade_date": trade_date.isoformat(),
            "rows_written": 0,
            "universe_size": 0,
            "skipped": True,
        }
    tuples = [
        (
            item["trade_date"],
            item["symbol"],
            item["close"],
            item["iv_rank_1y"],
            item["vrp_pct_252d"],
            item["atm_slope_30d"],
            item["pin_pct_distance"],
            item["dte_to_opex"],
            item["zero_gamma_offset"],
            item["gex_notional"],
            item["terrain_regime"],
            item["pin_score"],
            item["tail_risk"],
            item["trend_release"],
            item["composite_score"],
            item["lens_flags"],
            item["computed_at"],
        )
        for item in built
    ]
    written = batch_upsert(
        conn,
        TABLE_STOCK_SIGNAL_SCAN_DAILY,
        _SCAN_COLS,
        tuples,
        conflict_keys=("trade_date", "symbol"),
        set_fetched_at=True,
    )
    return {
        "trade_date": trade_date.isoformat(),
        "rows_written": written,
        "universe_size": len(built),
    }


def run(
    *,
    lookback_days: int = 3,
    symbols: Sequence[str] | None = None,
    as_of: date | None = None,
    dry_run: bool = False,
    single_symbol: str | None = None,
) -> dict[str, object]:
    day = as_of or _today_ny()
    conn = connect()
    try:
        underlyings = load_symbols_from_env_or_query(conn, symbols=symbols)
        watchlist = union_iv_radar_benchmarks(underlyings)

        if single_symbol:
            sym = single_symbol.strip().upper()
            one = compute_scan_for_date(
                conn,
                trade_date=day,
                watchlist=watchlist,
                symbols_filter=[sym],
                dry_run=dry_run,
            )
            return {
                "mode": "single-symbol",
                "symbol": sym,
                "trade_date": day.isoformat(),
                **one,
            }

        trading_days = fetch_recent_trading_days(conn, lookback_days, as_of=day)
        if not trading_days:
            return {
                "mode": "batch",
                "lookback_days": lookback_days,
                "skipped": True,
                "reason": "no trading days",
            }

        totals = {"rows_written": 0, "universe_size": 0, "days": []}
        for td in trading_days:
            one = compute_scan_for_date(
                conn,
                trade_date=td,
                watchlist=watchlist,
                dry_run=dry_run,
            )
            totals["rows_written"] += int(one.get("rows_written") or 0)
            totals["universe_size"] = max(
                totals["universe_size"],
                int(one.get("universe_size") or 0),
            )
            totals["days"].append(one)
        result: dict[str, object] = {
            "mode": "dry-run" if dry_run else "batch",
            "lookback_days": lookback_days,
            "symbols": len(watchlist),
            "trading_days": [d.isoformat() for d in trading_days],
            **totals,
        }
        logger.info(
            "scan days=%s..%s rows=%s universe=%s",
            trading_days[0],
            trading_days[-1],
            totals["rows_written"],
            totals["universe_size"],
        )
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Research materialized scanner (Wave D)")
    parser.add_argument("--lookback-days", type=int, default=3)
    parser.add_argument("--as-of", type=str, default="", help="YYYY-MM-DD (NY session)")
    parser.add_argument("--symbol", type=str, default="", help="Single-symbol run (skips batch)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    result = run(
        lookback_days=args.lookback_days,
        as_of=as_of,
        dry_run=args.dry_run,
        single_symbol=args.symbol or None,
    )
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
