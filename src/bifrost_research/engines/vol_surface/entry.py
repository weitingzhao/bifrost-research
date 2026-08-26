"""Vol Surface (SVI) CronJob entrypoint — Wave RS-B-Surface1.

Usage::

    python -m bifrost_research.engines.vol_surface.entry
    python -m bifrost_research.engines.vol_surface.entry --symbol NVDA --dry-run
    python -m bifrost_research.engines.vol_surface.entry --as-of 2026-08-22
"""

from __future__ import annotations

import argparse
import logging
import math
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
from bifrost_research.engines.vol_surface.fit import SviFitResult, fit_svi_smile
from bifrost_research.engines.volatility.surface import (
    IvPoint,
    fetch_iv_points_for_date,
    moneyness,
)

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")

_MIN_POINTS = 10
_MIN_DTE = 7
_MAX_DTE = 90

_FIT_COLS = (
    "symbol",
    "trade_date",
    "expiry",
    "dte",
    "svi_a",
    "svi_b",
    "svi_rho",
    "svi_m",
    "svi_sigma",
    "atm_vol",
    "atm_slope",
    "fit_rmse",
    "n_points",
    "computed_at",
)

_RESIDUAL_COLS = (
    "symbol",
    "trade_date",
    "expiry",
    "strike",
    "log_moneyness",
    "iv_market",
    "iv_fitted",
    "residual",
    "residual_z",
    "computed_at",
)


def _today_ny() -> date:
    return datetime.now(timezone.utc).astimezone(_NY).date()


def _dte(trade_date: date, expiry: date) -> int:
    return (expiry - trade_date).days


def _prepare_smile(
    points: Sequence[IvPoint],
    spot: float,
) -> tuple[list[float], list[float], list[float]] | None:
    """Return (log_moneyness, iv_market, strikes) filtered/ordered by k."""
    triples: list[tuple[float, float, float]] = []
    for p in points:
        if p.strike <= 0 or spot <= 0:
            continue
        if p.iv is None:
            continue
        try:
            iv = float(p.iv)
        except (TypeError, ValueError):
            continue
        if not (0.0 < iv < 5.0):
            continue
        k = moneyness(p.strike, spot)
        if not math.isfinite(k):
            continue
        triples.append((k, iv, p.strike))
    if len(triples) < _MIN_POINTS:
        return None
    triples.sort(key=lambda t: t[0])
    ks = [t[0] for t in triples]
    ivs = [t[1] for t in triples]
    strikes = [t[2] for t in triples]
    return ks, ivs, strikes


def compute_vol_surface_for_symbol(
    conn: Any,
    *,
    symbol: str,
    trade_date: date,
) -> dict[str, Any]:
    """Fit SVI per expiry for a single symbol on ``trade_date`` and persist."""
    sym = symbol.strip().upper()
    spot, by_exp = fetch_iv_points_for_date(conn, sym, trade_date)
    if spot is None or not by_exp:
        return {
            "ok": False,
            "symbol": sym,
            "trade_date": trade_date.isoformat(),
            "reason": "no snapshot points",
            "expiries_fit": 0,
            "expiries_skipped": 0,
        }

    now = datetime.now(timezone.utc)
    fit_rows: list[tuple[Any, ...]] = []
    residual_rows: list[tuple[Any, ...]] = []
    fits: list[SviFitResult] = []
    skipped = 0
    for expiry, pts in sorted(by_exp.items()):
        dte = _dte(trade_date, expiry)
        if dte < _MIN_DTE or dte > _MAX_DTE:
            skipped += 1
            continue
        prepared = _prepare_smile(pts, spot)
        if prepared is None:
            skipped += 1
            logger.info("vol-surface skip %s %s: n_points<%d", sym, expiry, _MIN_POINTS)
            continue
        ks, ivs, strikes = prepared
        T = max(dte, 1) / 365.0
        result = fit_svi_smile(ks, ivs, T)
        if result is None:
            skipped += 1
            logger.info("vol-surface skip %s %s: fit failed", sym, expiry)
            continue
        atm_vol = result.atm_vol()
        atm_slope = result.atm_slope()
        fit_rows.append(
            (
                sym,
                trade_date,
                expiry,
                dte,
                result.a,
                result.b,
                result.rho,
                result.m,
                result.sigma,
                round(atm_vol, 8) if math.isfinite(atm_vol) else None,
                round(atm_slope, 8) if math.isfinite(atm_slope) else None,
                result.rmse,
                result.n_points,
                now,
            )
        )
        rmse_safe = result.rmse if result.rmse and result.rmse > 0 else 1e-6
        for k, iv_mkt, iv_fit, strike in zip(
            result.log_moneyness, result.iv_market, result.iv_fitted, strikes
        ):
            residual = iv_mkt - iv_fit
            residual_rows.append(
                (
                    sym,
                    trade_date,
                    expiry,
                    strike,
                    round(k, 8),
                    round(iv_mkt, 8),
                    round(iv_fit, 8),
                    round(residual, 8),
                    round(residual / rmse_safe, 8),
                    now,
                )
            )
        fits.append(result)

    if fit_rows:
        batch_upsert(
            conn,
            "features.option_surface_fit_daily",
            _FIT_COLS,
            fit_rows,
            conflict_keys=("symbol", "trade_date", "expiry"),
            update_cols=(
                "dte",
                "svi_a",
                "svi_b",
                "svi_rho",
                "svi_m",
                "svi_sigma",
                "atm_vol",
                "atm_slope",
                "fit_rmse",
                "n_points",
                "computed_at",
            ),
            set_fetched_at=False,
        )
    if residual_rows:
        batch_upsert(
            conn,
            "features.option_surface_residual_daily",
            _RESIDUAL_COLS,
            residual_rows,
            conflict_keys=("symbol", "trade_date", "expiry", "strike"),
            update_cols=(
                "log_moneyness",
                "iv_market",
                "iv_fitted",
                "residual",
                "residual_z",
                "computed_at",
            ),
            set_fetched_at=False,
        )

    return {
        "ok": True,
        "symbol": sym,
        "trade_date": trade_date.isoformat(),
        "spot": spot,
        "expiries_fit": len(fit_rows),
        "expiries_skipped": skipped,
        "residual_rows_written": len(residual_rows),
        "arb_free_all": all(f.arb_free for f in fits) if fits else None,
    }


def compute_vol_surface_for_date(
    conn: Any,
    *,
    trade_date: date,
    underlyings: Sequence[str] | None = None,
) -> dict[str, Any]:
    syms = _resolve_symbols(underlyings)
    totals: dict[str, Any] = {
        "trade_date": trade_date.isoformat(),
        "symbols": len(syms),
        "expiries_fit": 0,
        "expiries_skipped": 0,
        "residual_rows": 0,
        "symbols_ok": 0,
        "symbols_skipped": 0,
    }
    for sym in syms:
        try:
            r = compute_vol_surface_for_symbol(conn, symbol=sym, trade_date=trade_date)
        except Exception:
            logger.exception("vol-surface compute failed for %s", sym)
            try:
                conn.rollback()
            except Exception:
                pass
            totals["symbols_skipped"] += 1
            continue
        if r.get("ok"):
            totals["symbols_ok"] += 1
            totals["expiries_fit"] += int(r.get("expiries_fit") or 0)
            totals["expiries_skipped"] += int(r.get("expiries_skipped") or 0)
            totals["residual_rows"] += int(r.get("residual_rows_written") or 0)
        else:
            totals["symbols_skipped"] += 1
    return totals


def _resolve_symbols(underlyings: Sequence[str] | None) -> list[str]:
    if underlyings:
        return sorted({str(s).strip().upper() for s in underlyings if str(s).strip()})
    return []


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
            spot, by_exp = fetch_iv_points_for_date(conn, sym, day)
            if dry_run:
                return {
                    "mode": "dry-run",
                    "symbol": sym,
                    "trade_date": day.isoformat(),
                    "spot": spot,
                    "expiries_available": len(by_exp or {}),
                }
            result = compute_vol_surface_for_symbol(conn, symbol=sym, trade_date=day)
            return {"mode": "single-symbol", **result}

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
                "sample_symbol": universe[0] if universe else None,
            }
        totals = {
            "expiries_fit": 0,
            "expiries_skipped": 0,
            "residual_rows": 0,
            "symbols_ok": 0,
            "symbols_skipped": 0,
            "days": [],
        }
        for td in trading_days:
            one = compute_vol_surface_for_date(
                conn,
                trade_date=td,
                underlyings=universe or None,
            )
            totals["expiries_fit"] += int(one.get("expiries_fit") or 0)
            totals["expiries_skipped"] += int(one.get("expiries_skipped") or 0)
            totals["residual_rows"] += int(one.get("residual_rows") or 0)
            totals["symbols_ok"] += int(one.get("symbols_ok") or 0)
            totals["symbols_skipped"] += int(one.get("symbols_skipped") or 0)
            totals["days"].append(one)
        result_out: dict[str, Any] = {
            "mode": "batch",
            "lookback_days": lookback_days,
            "symbols": len(universe),
            "trading_days": [d.isoformat() for d in trading_days],
            **totals,
        }
        logger.info(
            "vol-surface days=%s..%s expiries=%s residuals=%s symbols_ok=%s",
            trading_days[0],
            trading_days[-1],
            totals["expiries_fit"],
            totals["residual_rows"],
            totals["symbols_ok"],
        )
        return result_out
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
    parser = argparse.ArgumentParser(description="Research SVI Vol Surface (Wave RS-B-Surface1)")
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
