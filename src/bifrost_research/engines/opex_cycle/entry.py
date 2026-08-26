"""OpEx Cycle CronJob entrypoint — Wave RS-B-OpEx1.

Computes per-symbol totals of dealer Vanna and Charm exposure, plus the
zero-crossing strike for each metric and days-to-next-OpEx.

Usage::

    python -m bifrost_research.engines.opex_cycle.entry
    python -m bifrost_research.engines.opex_cycle.entry --symbol SPX --dry-run
    python -m bifrost_research.engines.opex_cycle.entry --as-of 2026-08-22
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from bifrost_research.db.calendar import (
    fetch_recent_trading_days,
    load_symbols_from_env_or_query,
    union_iv_radar_benchmarks,
)
from bifrost_research.db.conn import connect
from bifrost_research.db.upsert import batch_upsert
from bifrost_research.engines.gex.exposure import fetch_spot
from bifrost_research.engines.opex_cycle.calendar import (
    days_to_opex,
    is_opex_week,
)
from bifrost_research.engines.opex_cycle.vanna_charm import (
    ContractGreek,
    strike_vanna_charm_from_contracts,
    zero_crossing_strike,
)
from bifrost_research.engines.volatility.surface import fetch_iv_points_for_date

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")

_MIN_DTE = 7
_MAX_DTE = 90

_COLS = (
    "symbol",
    "trade_date",
    "spot",
    "total_vanna",
    "total_charm",
    "vanna_zero_strike",
    "charm_zero_strike",
    "dte_to_opex",
    "is_opex_week",
    "computed_at",
)


def _today_ny() -> date:
    return datetime.now(timezone.utc).astimezone(_NY).date()


def compute_opex_for_symbol(
    conn: Any,
    *,
    symbol: str,
    trade_date: date,
) -> dict[str, Any]:
    """Compute OpEx cycle metrics for a single symbol and persist."""
    sym = symbol.strip().upper()

    spot, by_exp = fetch_iv_points_for_date(conn, sym, trade_date)
    if spot is None or not by_exp:
        fallback_spot = fetch_spot(conn, sym, trade_date)
        if fallback_spot is None:
            return {
                "ok": False,
                "symbol": sym,
                "trade_date": trade_date.isoformat(),
                "reason": "no spot",
            }
        spot = fallback_spot

    contracts: list[ContractGreek] = []
    for expiry, pts in by_exp.items():
        dte = (expiry - trade_date).days
        if dte < _MIN_DTE or dte > _MAX_DTE:
            continue
        t_years = max(dte, 1) / 365.0
        for p in pts:
            try:
                iv = float(p.iv)
            except (TypeError, ValueError):
                continue
            if not (0.0 < iv < 5.0):
                continue
            contracts.append(
                ContractGreek(
                    strike=float(p.strike),
                    option_right=str(getattr(p, "option_right", "") or ""),
                    open_interest=1,
                    iv=iv,
                    t_years=t_years,
                )
            )

    if not contracts:
        return {
            "ok": False,
            "symbol": sym,
            "trade_date": trade_date.isoformat(),
            "reason": "no contracts in DTE window",
        }

    dist = strike_vanna_charm_from_contracts(contracts, spot)
    total_vanna = sum(r["net_vanna"] for r in dist)
    total_charm = sum(r["net_charm"] for r in dist)
    vanna_zero = zero_crossing_strike(dist, "net_vanna", spot)
    charm_zero = zero_crossing_strike(dist, "net_charm", spot)
    dte_opex = days_to_opex(trade_date)
    opex_week = is_opex_week(trade_date)

    now = datetime.now(timezone.utc)
    batch_upsert(
        conn,
        "features.option_metric_vanna_charm_daily",
        _COLS,
        [
            (
                sym,
                trade_date,
                float(spot),
                float(total_vanna),
                float(total_charm),
                vanna_zero,
                charm_zero,
                int(dte_opex),
                bool(opex_week),
                now,
            )
        ],
        conflict_keys=("symbol", "trade_date"),
        update_cols=(
            "spot",
            "total_vanna",
            "total_charm",
            "vanna_zero_strike",
            "charm_zero_strike",
            "dte_to_opex",
            "is_opex_week",
            "computed_at",
        ),
        set_fetched_at=False,
    )

    return {
        "ok": True,
        "symbol": sym,
        "trade_date": trade_date.isoformat(),
        "spot": float(spot),
        "total_vanna": round(float(total_vanna), 4),
        "total_charm": round(float(total_charm), 4),
        "vanna_zero_strike": vanna_zero,
        "charm_zero_strike": charm_zero,
        "dte_to_opex": int(dte_opex),
        "is_opex_week": bool(opex_week),
        "strikes": len(dist),
    }


def compute_opex_for_date(
    conn: Any,
    *,
    trade_date: date,
    underlyings: Sequence[str] | None = None,
) -> dict[str, Any]:
    syms = sorted({str(s).strip().upper() for s in (underlyings or []) if str(s).strip()})
    totals = {
        "trade_date": trade_date.isoformat(),
        "symbols": len(syms),
        "symbols_ok": 0,
        "symbols_skipped": 0,
    }
    for sym in syms:
        try:
            r = compute_opex_for_symbol(conn, symbol=sym, trade_date=trade_date)
        except Exception:
            logger.exception("opex compute failed for %s", sym)
            try:
                conn.rollback()
            except Exception:
                pass
            totals["symbols_skipped"] += 1
            continue
        if r.get("ok"):
            totals["symbols_ok"] += 1
        else:
            totals["symbols_skipped"] += 1
    return totals


def run(
    *,
    lookback_days: int = 1,
    symbols: Sequence[str] | None = None,
    as_of: date | None = None,
    dry_run: bool = False,
    single_symbol: str | None = None,
) -> dict[str, Any]:
    day = as_of or _today_ny()
    conn = connect()
    try:
        underlyings = load_symbols_from_env_or_query(conn, symbols=symbols)
        universe = union_iv_radar_benchmarks(underlyings)

        if single_symbol:
            sym = single_symbol.strip().upper()
            if dry_run:
                spot, by_exp = fetch_iv_points_for_date(conn, sym, day)
                return {
                    "mode": "dry-run",
                    "symbol": sym,
                    "trade_date": day.isoformat(),
                    "spot": spot,
                    "expiries_available": len(by_exp or {}),
                    "dte_to_opex": days_to_opex(day),
                    "is_opex_week": is_opex_week(day),
                }
            r = compute_opex_for_symbol(conn, symbol=sym, trade_date=day)
            return {"mode": "single-symbol", **r}

        trading_days = fetch_recent_trading_days(conn, lookback_days, as_of=day)
        if not trading_days:
            return {
                "mode": "batch",
                "lookback_days": lookback_days,
                "skipped": True,
                "reason": "no trading days",
            }

        if dry_run:
            return {
                "mode": "dry-run",
                "trading_days": [d.isoformat() for d in trading_days],
                "symbols": len(universe),
                "dte_to_opex": days_to_opex(day),
                "is_opex_week": is_opex_week(day),
            }

        totals = {"symbols_ok": 0, "symbols_skipped": 0, "days": []}
        for td in trading_days:
            one = compute_opex_for_date(
                conn,
                trade_date=td,
                underlyings=universe or None,
            )
            totals["symbols_ok"] += int(one.get("symbols_ok") or 0)
            totals["symbols_skipped"] += int(one.get("symbols_skipped") or 0)
            totals["days"].append(one)
        result: dict[str, Any] = {
            "mode": "batch",
            "lookback_days": lookback_days,
            "symbols": len(universe),
            "trading_days": [d.isoformat() for d in trading_days],
            **totals,
        }
        logger.info(
            "opex days=%s..%s ok=%s skipped=%s",
            trading_days[0],
            trading_days[-1],
            totals["symbols_ok"],
            totals["symbols_skipped"],
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
    parser = argparse.ArgumentParser(description="Research OpEx Vanna/Charm (Wave RS-B-OpEx1)")
    parser.add_argument("--lookback-days", type=int, default=1)
    parser.add_argument("--as-of", type=str, default="", help="YYYY-MM-DD (NY session)")
    parser.add_argument("--symbol", type=str, default="", help="Single-symbol run")
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
