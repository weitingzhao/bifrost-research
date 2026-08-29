"""Wave 3–4 engine scheduler — CronJob entrypoints for research NS.

Slots:
  momentum / gex / iv-surface / flow  — Wave 3 (real engine compute)
  terrain / forecast                  — Wave 4 (terrain from upstream tables)

D10 BLOCKED — advisory writes to research.* only.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from bifrost_research.db.calendar import (
    fetch_recent_trading_days,
    load_symbols_from_env_or_query,
)
from bifrost_research.db.conn import connect
from bifrost_research.engines.flow import compute_order_flow_for_symbol
from bifrost_research.engines.forecast.playbook import (
    build_forecast_session,
    upsert_forecast_session,
)
from bifrost_research.engines.forecast.terrain import (
    compute_market_terrain,
    load_upstream_signals,
    upsert_market_terrain,
)
from bifrost_research.engines.backtest.settlement import (
    load_actual_close,
    settle_forecast,
    upsert_settlement,
)
from bifrost_research.engines.gex.exposure import compute_gex_for_symbol, compute_gex_intraday
from bifrost_research.engines.momentum.radar import compute_momentum_for_date
from bifrost_research.engines.volatility.surface import compute_iv_surface_for_symbol

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")

SLOT_NAMES = (
    "momentum",
    "gex",
    "iv-surface",
    "flow",
    "terrain",
    "forecast",
    "terrain-intraday",
    "gex-intraday",
    "settlement",
)


def _today_ny() -> date:
    return datetime.now(timezone.utc).astimezone(_NY).date()


def run_momentum(
    conn: Any,
    *,
    trading_days: Sequence[date],
    symbols: Sequence[str],
) -> dict[str, Any]:
    written = 0
    skipped = 0
    for td in trading_days:
        result = compute_momentum_for_date(conn, trade_date=td, symbols=symbols)
        written += int(result.get("rows_written") or 0)
        skipped += int(result.get("skipped") or 0)
    return {
        "slot": "momentum",
        "rows_written": written,
        "skipped": skipped,
        "symbols": len(symbols),
        "trading_days": [d.isoformat() for d in trading_days],
    }


def run_gex(
    conn: Any,
    *,
    trading_days: Sequence[date],
    symbols: Sequence[str],
) -> dict[str, Any]:
    written = 0
    ok = 0
    failed = 0
    for td in trading_days:
        for sym in symbols:
            result = compute_gex_for_symbol(conn, symbol=sym, trade_date=td)
            if result.get("ok"):
                ok += 1
                written += int(result.get("distribution_rows") or 0)
            else:
                failed += 1
    return {
        "slot": "gex",
        "rows_written": written,
        "symbols_ok": ok,
        "symbols_failed": failed,
        "symbols": len(symbols),
        "trading_days": [d.isoformat() for d in trading_days],
    }


def run_iv_surface(
    conn: Any,
    *,
    trading_days: Sequence[date],
    symbols: Sequence[str],
) -> dict[str, Any]:
    written = 0
    ok = 0
    failed = 0
    for td in trading_days:
        for sym in symbols:
            result = compute_iv_surface_for_symbol(conn, symbol=sym, trade_date=td)
            if result.get("ok"):
                ok += 1
                written += int(result.get("rows_written") or 0)
            else:
                failed += 1
    return {
        "slot": "iv-surface",
        "rows_written": written,
        "symbols_ok": ok,
        "symbols_failed": failed,
        "symbols": len(symbols),
        "trading_days": [d.isoformat() for d in trading_days],
    }


def run_flow(
    conn: Any,
    *,
    trading_days: Sequence[date],
    symbols: Sequence[str],
) -> dict[str, Any]:
    """Order-flow: prefer market.option_trades tape; else snapshot/OI proxy."""
    written = 0
    ok = 0
    failed = 0
    tape_ok = 0
    proxy_ok = 0
    for td in trading_days:
        for sym in symbols:
            result = compute_order_flow_for_symbol(conn, symbol=sym, trade_date=td)
            if result.get("ok"):
                ok += 1
                written += 1
                if result.get("data_source") == "option_trades_tape":
                    tape_ok += 1
                else:
                    proxy_ok += 1
            else:
                failed += 1
    return {
        "slot": "flow",
        "rows_written": written,
        "symbols_ok": ok,
        "symbols_failed": failed,
        "symbols_tape": tape_ok,
        "symbols_proxy": proxy_ok,
        "symbols": len(symbols),
        "trading_days": [d.isoformat() for d in trading_days],
        "data_source": "option_trades_tape_or_proxy",
    }


def run_terrain(
    conn: Any,
    *,
    trading_days: Sequence[date],
    symbols: Sequence[str],
) -> dict[str, Any]:
    written = 0
    skipped = 0
    for td in trading_days:
        for sym in symbols:
            spot, gex, momentum, iv = load_upstream_signals(conn, sym, td)
            if spot <= 0:
                skipped += 1
                continue
            terrain = compute_market_terrain(
                sym,
                td,
                spot=spot,
                gex=gex or None,
                momentum=momentum or None,
                iv=iv or None,
            )
            written += upsert_market_terrain(conn, [terrain])
    return {
        "slot": "terrain",
        "rows_written": written,
        "skipped_no_spot": skipped,
        "symbols": len(symbols),
        "trading_days": [d.isoformat() for d in trading_days],
    }


def run_forecast(
    conn: Any,
    *,
    trading_days: Sequence[date],
    symbols: Sequence[str],
) -> dict[str, Any]:
    written = 0
    skipped = 0
    for td in trading_days:
        for sym in symbols:
            spot, gex, momentum, iv = load_upstream_signals(conn, sym, td)
            if spot <= 0:
                skipped += 1
                continue
            terrain = compute_market_terrain(
                sym,
                td,
                spot=spot,
                gex=gex or None,
                momentum=momentum or None,
                iv=iv or None,
            )
            session = build_forecast_session(terrain, enrich=True)
            written += upsert_forecast_session(conn, session)
            upsert_market_terrain(conn, [terrain])
    return {
        "slot": "forecast",
        "rows_written": written,
        "skipped_no_spot": skipped,
        "symbols": len(symbols),
        "trading_days": [d.isoformat() for d in trading_days],
    }


def run_terrain_intraday(
    conn: Any,
    *,
    trading_days: Sequence[date],
    symbols: Sequence[str],
) -> dict[str, Any]:
    from bifrost_research.engines.forecast.terrain import (
        compute_terrain_intraday,
        upsert_terrain_intraday,
    )

    now_utc = datetime.now(timezone.utc)
    written = 0
    skipped = 0
    today = _today_ny()
    for sym in symbols:
        spot, gex, momentum, iv = load_upstream_signals(conn, sym, today)
        if spot <= 0:
            skipped += 1
            continue
        terrain = compute_terrain_intraday(
            sym,
            today,
            now_utc,
            spot=spot,
            gex=gex or None,
            momentum=momentum or None,
            iv=iv or None,
        )
        written += upsert_terrain_intraday(conn, [terrain])
        try:
            from bifrost_research.engines.forecast.playbook import (
                emit_triggers_for_terrain_intraday,
            )

            emit_triggers_for_terrain_intraday(
                conn,
                symbol=terrain.symbol,
                trade_date=terrain.trade_date,
                asof_ts=terrain.asof_ts,
                regime=str(terrain.regime),
                prob_rangy=terrain.prob_rangy,
                prob_bull=terrain.prob_bull,
                prob_bear=terrain.prob_bear,
                prob_squeeze=terrain.prob_squeeze,
            )
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
    return {
        "slot": "terrain-intraday",
        "rows_written": written,
        "skipped_no_spot": skipped,
        "symbols": len(symbols),
        "asof_ts": now_utc.isoformat(),
    }


def run_gex_intraday(
    conn: Any,
    *,
    trading_days: Sequence[date],
    symbols: Sequence[str],
) -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    today = _today_ny()
    ok = 0
    failed = 0
    for sym in symbols:
        result = compute_gex_intraday(conn, symbol=sym, trade_date=today, asof_ts=now_utc)
        if result.get("ok"):
            ok += 1
        else:
            failed += 1
    return {
        "slot": "gex-intraday",
        "symbols_ok": ok,
        "symbols_failed": failed,
        "symbols": len(symbols),
        "asof_ts": now_utc.isoformat(),
    }


def run_settlement(
    conn: Any,
    *,
    trading_days: Sequence[date],
    symbols: Sequence[str],
) -> dict[str, Any]:
    """Settle all unsettled forecast sessions for the given trading days."""
    settled = 0
    skipped = 0
    for td in trading_days:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fs.session_id, fs.symbol, fs.trade_date,
                       fs.expected_close, fs.prob_rangy, fs.prob_bull,
                       fs.prob_bear, fs.prob_squeeze
                FROM features.stock_forecast_session fs
                LEFT JOIN features.stock_backtest_settlement stl
                    ON stl.session_id = fs.session_id
                WHERE fs.trade_date = %s AND stl.settlement_id IS NULL
                """,
                (td,),
            )
            rows = cur.fetchall() or []

        for row in rows:
            if isinstance(row, dict):
                sid, sym, tdate = row["session_id"], row["symbol"], row["trade_date"]
                expected = float(row["expected_close"] or 0)
            else:
                sid, sym, tdate, expected = row[0], row[1], row[2], float(row[3] or 0)

            actual = load_actual_close(conn, sym, tdate)
            if actual is None:
                skipped += 1
                continue

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT hour_et, path_call, level_low, level_high, level_target
                    FROM features.stock_forecast_hourly
                    WHERE session_id = %s ORDER BY hour_et
                    """,
                    (sid,),
                )
                hourly_rows = cur.fetchall() or []
            hourly: list[dict[str, Any]] = []
            for hr in hourly_rows:
                if isinstance(hr, dict):
                    hourly.append(hr)
                else:
                    hourly.append({
                        "hour_et": hr[0],
                        "path_call": hr[1],
                        "level_low": hr[2],
                        "level_high": hr[3],
                        "level_target": hr[4],
                    })

            stl = settle_forecast(
                session_id=sid,
                symbol=sym,
                trade_date=tdate if isinstance(tdate, date) else date.fromisoformat(str(tdate)),
                expected_close=expected,
                hourly=hourly,
                actual_close=actual,
            )
            upsert_settlement(conn, stl)
            settled += 1

    return {
        "slot": "settlement",
        "sessions_settled": settled,
        "skipped_no_actual": skipped,
        "trading_days": [d.isoformat() for d in trading_days],
    }


_SLOT_RUNNERS = {
    "momentum": run_momentum,
    "gex": run_gex,
    "iv-surface": run_iv_surface,
    "flow": run_flow,
    "terrain": run_terrain,
    "forecast": run_forecast,
    "terrain-intraday": run_terrain_intraday,
    "gex-intraday": run_gex_intraday,
    "settlement": run_settlement,
}


def run_slot(
    slot: str,
    *,
    lookback_days: int = 2,
    symbols: Sequence[str] | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    day = as_of or _today_ny()
    runner = _SLOT_RUNNERS.get(slot)
    if runner is None:
        raise ValueError(f"unknown slot: {slot}")

    conn = connect()
    try:
        underlyings = load_symbols_from_env_or_query(conn, symbols=symbols)
        trading_days = fetch_recent_trading_days(conn, lookback_days, as_of=day)
        if not trading_days:
            return {
                "slot": slot,
                "skipped": True,
                "reason": "no trading days",
            }
        if not underlyings:
            return {
                "slot": slot,
                "skipped": True,
                "reason": "no symbols",
            }
        return runner(conn, trading_days=trading_days, symbols=underlyings)
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research Wave 3–4 engine scheduler")
    parser.add_argument("--slot", required=True, choices=SLOT_NAMES)
    parser.add_argument("--lookback-days", type=int, default=2)
    parser.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    try:
        result = run_slot(args.slot, lookback_days=args.lookback_days, as_of=as_of)
    except Exception:
        logger.exception("scheduler failed slot=%s", args.slot)
        return 1
    logger.info("result=%s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
