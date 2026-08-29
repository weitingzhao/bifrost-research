"""Historical IV solver — Brent BS inversion + dual-source projection (IDS).

Sources:
  1. ``raw_market.option_daily`` OHLCV → Brent invert → ``solver_status=ok|…``
  2. ``raw_market.option_snapshot`` vendor IV → ``solver_status=vendor_snapshot``

Writes ``features.option_iv_reconstructed_daily``. See ``docs/IV_SOLVER_SPEC.md``.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from typing import Any, Literal, Sequence

from bifrost_research.db.upsert import batch_upsert
from bifrost_research.engines.backtest.canonical_pnl import bs_delta, bs_price
from bifrost_research.schema.schemas import TABLE_OPTION_IV_RECONSTRUCTED_DAILY

logger = logging.getLogger(__name__)

SolverStatus = Literal[
    "ok",
    "no_convergence",
    "insufficient_inputs",
    "vendor_snapshot",
]

_COLS = (
    "symbol",
    "option_ticker",
    "trade_date",
    "strike",
    "expiry",
    "option_right",
    "mid_price",
    "spot",
    "tte_years",
    "iv",
    "delta",
    "gamma",
    "solver_status",
    "computed_at",
)

STRIKE_LO = 0.80
STRIKE_HI = 1.20
DTE_MIN = 5
DTE_MAX = 90
IV_LO = 0.01
IV_HI = 5.0
BRENT_TOL = 1e-4
BRENT_MAXITER = 100


def bs_gamma(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    *,
    rate: float = 0.0,
) -> float:
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 1e-8:
        return 0.0
    vol = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * t_years) / vol
    return math.exp(-0.5 * d1 * d1) / (math.sqrt(2.0 * math.pi) * spot * vol)


def solve_iv(
    spot: float,
    strike: float,
    tte_years: float,
    mid: float,
    right: Literal["C", "P"],
    *,
    rate: float = 0.0,
) -> tuple[float | None, SolverStatus]:
    """Invert Black–Scholes mid → IV via Brent. Returns (iv, status)."""
    if (
        spot <= 0
        or strike <= 0
        or tte_years <= 1e-8
        or mid is None
        or mid <= 0
    ):
        return None, "insufficient_inputs"

    # Intrinsic floor — mid below intrinsic cannot be priced with r=0 BS.
    intrinsic = max(0.0, (spot - strike) if right == "C" else (strike - spot))
    if mid < intrinsic * 0.999:
        return None, "insufficient_inputs"

    def objective(sigma: float) -> float:
        return bs_price(spot, strike, tte_years, sigma, right=right, rate=rate) - mid

    a, b = IV_LO, IV_HI
    fa, fb = objective(a), objective(b)
    # Expand upper bracket if needed (deep OTM / high premium)
    expand = 0
    while fa * fb > 0 and expand < 8:
        b *= 1.5
        if b > 10.0:
            break
        fb = objective(b)
        expand += 1
    if fa * fb > 0:
        return None, "no_convergence"

    # Brent (simplified: scipy-free)
    c, fc = a, fa
    d = e = b - a
    for _ in range(BRENT_MAXITER):
        if fb * fc > 0:
            c, fc = a, fa
            d = e = b - a
        if abs(fc) < abs(fb):
            a, b, c = b, c, b
            fa, fb, fc = fb, fc, fb
        tol1 = 2.0 * BRENT_TOL * abs(b) + 0.5 * BRENT_TOL
        xm = 0.5 * (c - b)
        if abs(xm) <= tol1 or abs(fb) <= BRENT_TOL * max(1.0, abs(mid)):
            if IV_LO <= b <= IV_HI * 2:
                return float(b), "ok"
            return None, "no_convergence"
        if abs(e) >= tol1 and abs(fa) > abs(fb):
            s = fb / fa
            if a == c:
                p = 2.0 * xm * s
                q = 1.0 - s
            else:
                q = fa / fc
                r = fb / fc
                p = s * (2.0 * xm * q * (q - r) - (b - a) * (r - 1.0))
                q = (q - 1.0) * (r - 1.0) * (s - 1.0)
            if p > 0:
                q = -q
            p = abs(p)
            min1 = 3.0 * xm * q - abs(tol1 * q)
            min2 = abs(e * q)
            if 2.0 * p < min(min1, min2):
                e = d
                d = p / q
            else:
                d = e = xm
        else:
            d = e = xm
        a, fa = b, fb
        if abs(d) > tol1:
            b += d
        else:
            b += math.copysign(tol1, xm)
        fb = objective(b)
    return None, "no_convergence"


def _mid_from_ohlc(close: Any, high: Any, low: Any) -> float | None:
    try:
        if close is not None and float(close) > 0:
            return float(close)
    except (TypeError, ValueError):
        pass
    try:
        if high is not None and low is not None:
            h, lo = float(high), float(low)
            if h > 0 and lo > 0:
                return 0.5 * (h + lo)
    except (TypeError, ValueError):
        pass
    return None


def _passes_filters(spot: float, strike: float, dte: int) -> bool:
    if dte < DTE_MIN or dte > DTE_MAX:
        return False
    if spot <= 0 or strike <= 0:
        return False
    return STRIKE_LO * spot <= strike <= STRIKE_HI * spot


def _right_lit(raw: Any) -> Literal["C", "P"] | None:
    s = str(raw or "").strip().upper()
    if s in ("C", "CALL"):
        return "C"
    if s in ("P", "PUT"):
        return "P"
    return None


def upsert_reconstructed(conn: Any, rows: Sequence[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    return batch_upsert(
        conn,
        TABLE_OPTION_IV_RECONSTRUCTED_DAILY,
        _COLS,
        rows,
        conflict_keys=("symbol", "option_ticker", "trade_date"),
        update_cols=(
            "strike",
            "expiry",
            "option_right",
            "mid_price",
            "spot",
            "tte_years",
            "iv",
            "delta",
            "gamma",
            "solver_status",
            "computed_at",
        ),
        set_fetched_at=False,
    )


def solve_symbol_window(
    conn: Any,
    symbol: str,
    start_date: date,
    end_date: date,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Brent-invert option_daily OHLCV for one symbol over [start, end]."""
    sym = symbol.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.option_ticker, o.underlying, o.bar_date, o.expiry, o.strike,
                   o.option_right, o.open, o.high, o.low, o.close, s.close AS spot
            FROM raw_market.option_daily o
            JOIN raw_market.stock_daily s
              ON s.symbol = o.underlying AND s.bar_date = o.bar_date
            WHERE UPPER(TRIM(o.underlying)) = %s
              AND o.bar_date BETWEEN %s AND %s
              AND s.close IS NOT NULL AND s.close > 0
            ORDER BY o.bar_date, o.option_ticker
            """,
            (sym, start_date, end_date),
        )
        raw = cur.fetchall() or []

    now = datetime.now(timezone.utc)
    out_rows: list[tuple[Any, ...]] = []
    status_counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []

    for r in raw:
        ticker, und, bar_d, expiry, strike, right_raw = r[0], r[1], r[2], r[3], r[4], r[5]
        high, low, close, spot = r[7], r[8], r[9], r[10]
        right = _right_lit(right_raw)
        mid = _mid_from_ohlc(close, high, low)
        try:
            strike_f = float(strike)
            spot_f = float(spot)
        except (TypeError, ValueError):
            continue
        if right is None or mid is None or expiry is None or bar_d is None:
            continue
        dte = (expiry - bar_d).days
        if not _passes_filters(spot_f, strike_f, dte):
            continue
        tte = max(dte, 1) / 365.0
        iv, status = solve_iv(spot_f, strike_f, tte, mid, right)
        status_counts[status] = status_counts.get(status, 0) + 1
        delta = gamma = None
        if iv is not None:
            delta = bs_delta(spot_f, strike_f, tte, iv, right=right)
            gamma = bs_gamma(spot_f, strike_f, tte, iv)
        row = (
            str(und).strip().upper(),
            str(ticker),
            bar_d,
            strike_f,
            expiry,
            right,
            mid,
            spot_f,
            tte,
            iv,
            delta,
            gamma,
            status,
            now,
        )
        out_rows.append(row)
        if len(samples) < 3:
            samples.append(
                {
                    "option_ticker": ticker,
                    "trade_date": bar_d.isoformat() if hasattr(bar_d, "isoformat") else str(bar_d),
                    "strike": strike_f,
                    "mid": mid,
                    "spot": spot_f,
                    "iv": iv,
                    "solver_status": status,
                }
            )

    if dry_run:
        return {
            "symbol": sym,
            "source": "option_daily",
            "dry_run": True,
            "rows": len(out_rows),
            "by_status": status_counts,
            "sample": samples,
        }
    n = upsert_reconstructed(conn, out_rows)
    return {
        "symbol": sym,
        "source": "option_daily",
        "rows_written": n,
        "by_status": status_counts,
    }


def project_vendor_snapshot_window(
    conn: Any,
    symbol: str,
    start_date: date,
    end_date: date,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Project Polygon snapshot IV into reconstructed table (depth path)."""
    sym = symbol.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (v.option_ticker, DATE(timezone('America/New_York', v.snapshot_ts)))
              v.option_ticker,
              UPPER(TRIM(v.underlying)),
              DATE(timezone('America/New_York', v.snapshot_ts)) AS trade_date,
              oc.expiry,
              oc.strike,
              oc.option_right,
              v.iv,
              v.underlying_price AS spot,
              v.delta,
              v.gamma
            FROM raw_market.v_option_snapshot_with_stock v
            INNER JOIN raw_market.option_contract oc
              ON oc.option_ticker = v.option_ticker
            WHERE UPPER(TRIM(v.underlying)) = %s
              AND DATE(timezone('America/New_York', v.snapshot_ts)) BETWEEN %s AND %s
              AND v.iv IS NOT NULL AND v.iv > 0
              AND v.underlying_price IS NOT NULL AND v.underlying_price > 0
            ORDER BY v.option_ticker,
                     DATE(timezone('America/New_York', v.snapshot_ts)),
                     v.snapshot_ts DESC
            """,
            (sym, start_date, end_date),
        )
        raw = cur.fetchall() or []

    now = datetime.now(timezone.utc)
    out_rows: list[tuple[Any, ...]] = []
    for r in raw:
        ticker, und, trade_d, expiry, strike, right_raw = r[0], r[1], r[2], r[3], r[4], r[5]
        iv, spot, delta, gamma = r[6], r[7], r[8], r[9]
        right = _right_lit(right_raw)
        try:
            strike_f = float(strike)
            spot_f = float(spot)
            iv_f = float(iv)
        except (TypeError, ValueError):
            continue
        if right is None or expiry is None or trade_d is None:
            continue
        dte = (expiry - trade_d).days
        # Vendor path: keep all positive-DTE contracts with sane IV (depth).
        # Moneyness/DTE filters apply only to OHLCV Brent path.
        if dte < 1:
            continue
        # Normalize percent-style IV if vendor stored > 3
        if iv_f > 3.0:
            iv_f = iv_f / 100.0
        if not (IV_LO <= iv_f <= IV_HI):
            continue
        tte = max(dte, 1) / 365.0
        # Approximate mid from BS for audit trail
        mid = bs_price(spot_f, strike_f, tte, iv_f, right=right)
        out_rows.append(
            (
                und,
                str(ticker),
                trade_d,
                strike_f,
                expiry,
                right,
                mid,
                spot_f,
                tte,
                iv_f,
                float(delta) if delta is not None else bs_delta(spot_f, strike_f, tte, iv_f, right=right),
                float(gamma) if gamma is not None else bs_gamma(spot_f, strike_f, tte, iv_f),
                "vendor_snapshot",
                now,
            )
        )

    if dry_run:
        return {
            "symbol": sym,
            "source": "option_snapshot",
            "dry_run": True,
            "rows": len(out_rows),
            "sample": [
                {
                    "option_ticker": r[1],
                    "trade_date": r[2].isoformat() if hasattr(r[2], "isoformat") else str(r[2]),
                    "iv": r[9],
                    "solver_status": r[12],
                }
                for r in out_rows[:3]
            ],
        }
    n = upsert_reconstructed(conn, out_rows)
    return {
        "symbol": sym,
        "source": "option_snapshot",
        "rows_written": n,
    }


def run_cohort(
    conn: Any,
    *,
    symbols: Sequence[str],
    lookback_days: int = 252,
    as_of: date | None = None,
    source: Literal["all", "daily", "snapshot"] = "all",
    dry_run: bool = False,
) -> dict[str, Any]:
    end = as_of or date.today()
    start = end.fromordinal(end.toordinal() - int(lookback_days * 1.5))  # calendar buffer
    # Prefer trading-day lookback via stock calendar when available
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bar_date FROM raw_market.stock_daily
                WHERE symbol = 'SPY' AND bar_date <= %s
                ORDER BY bar_date DESC
                LIMIT %s
                """,
                (end, lookback_days),
            )
            days = [r[0] for r in (cur.fetchall() or [])]
            if days:
                start = min(days)
    except Exception:
        conn.rollback()

    per: list[dict[str, Any]] = []
    total = 0
    for sym in symbols:
        if source in ("all", "snapshot"):
            one = project_vendor_snapshot_window(
                conn, sym, start, end, dry_run=dry_run
            )
            per.append(one)
            total += int(one.get("rows_written") or one.get("rows") or 0)
        if source in ("all", "daily"):
            one = solve_symbol_window(conn, sym, start, end, dry_run=dry_run)
            per.append(one)
            total += int(one.get("rows_written") or one.get("rows") or 0)

    coverage = None
    if not dry_run:
        coverage = coverage_report(conn)
    return {
        "mode": "cohort",
        "lookback_days": lookback_days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "source": source,
        "symbols": len(symbols),
        "rows_written": total,
        "per_symbol": per,
        "coverage": coverage,
        "dry_run": dry_run,
    }


def coverage_report(conn: Any) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)::bigint,
                   COUNT(DISTINCT symbol)::bigint,
                   COUNT(DISTINCT trade_date)::bigint
            FROM {TABLE_OPTION_IV_RECONSTRUCTED_DAILY}
            """
        )
        row = cur.fetchone() or (0, 0, 0)
        cur.execute(
            f"""
            SELECT solver_status, COUNT(*)::bigint
            FROM {TABLE_OPTION_IV_RECONSTRUCTED_DAILY}
            GROUP BY 1
            """
        )
        by_status = {str(r[0]): int(r[1]) for r in cur.fetchall()}
        cur.execute(
            f"""
            SELECT COUNT(*)::bigint
            FROM {TABLE_OPTION_IV_RECONSTRUCTED_DAILY}
            WHERE iv IS NOT NULL
            """
        )
        with_iv = int((cur.fetchone() or (0,))[0] or 0)
    total = int(row[0] or 0)
    ok = int(by_status.get("ok") or 0) + int(by_status.get("vendor_snapshot") or 0)
    return {
        "rows": total,
        "symbols": int(row[1] or 0),
        "distinct_dates": int(row[2] or 0),
        "with_iv": with_iv,
        "by_status": by_status,
        "solver_ok_pct": (ok / total) if total else None,
    }


__all__ = [
    "solve_iv",
    "bs_gamma",
    "solve_symbol_window",
    "project_vendor_snapshot_window",
    "run_cohort",
    "coverage_report",
    "upsert_reconstructed",
]
