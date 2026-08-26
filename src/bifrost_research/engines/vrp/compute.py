"""IV-RV Volatility Risk Premium daily compute.

RV: annualized standard deviation of close-to-close log returns over rolling
    windows (20d / 60d / 252d, √252 annualization).

VRP:
    vrp_20d = atm_iv_30d - rv_20d
    vrp_60d = atm_iv_30d - rv_60d

VRP percentile: rolling 252-trading-day rank of ``vrp_60d`` (0..100).

fwd_ret_20d is intentionally left NULL by this compute path; a later job will
backfill once 20 trading days have elapsed.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Mapping, Sequence

from bifrost_research.db.upsert import batch_upsert

_TRADING_DAYS_PER_YEAR = 252

_COLS = (
    "symbol",
    "trade_date",
    "rv_20d",
    "rv_60d",
    "rv_252d",
    "atm_iv_30d",
    "vrp_20d",
    "vrp_60d",
    "vrp_pct_252d",
    "fwd_ret_20d",
    "computed_at",
)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()[:10]
    if not s:
        return None
    return date.fromisoformat(s)


def _row_to_dict(row: Any, columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {columns[i]: row[i] for i in range(min(len(columns), len(row)))}


def annualized_close_to_close_rv(
    closes: Sequence[float],
    window: int,
    *,
    trading_days_per_year: int = _TRADING_DAYS_PER_YEAR,
) -> float | None:
    """Annualized RV = sample stdev of log(close_t / close_{t-1}) × √N.

    ``closes`` must be ordered oldest → newest. Uses the trailing ``window``
    returns (i.e. the trailing ``window + 1`` closes). Returns None when there
    are not enough valid closes to form ``window`` log returns.
    """
    if window < 2:
        raise ValueError("window must be >= 2")
    n_needed = window + 1
    valid_prices: list[float] = []
    for c in closes:
        try:
            val = float(c)
        except (TypeError, ValueError):
            continue
        if val > 0 and math.isfinite(val):
            valid_prices.append(val)
    if len(valid_prices) < n_needed:
        return None
    tail = valid_prices[-n_needed:]
    log_returns: list[float] = []
    prev = tail[0]
    for cur in tail[1:]:
        try:
            log_returns.append(math.log(cur / prev))
        except (ValueError, ZeroDivisionError):
            return None
        prev = cur
    if len(log_returns) < window:
        return None
    mean = sum(log_returns) / len(log_returns)
    # Sample stdev with Bessel correction (denominator N-1)
    if len(log_returns) < 2:
        return None
    var = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    stdev = math.sqrt(max(var, 0.0))
    return round(stdev * math.sqrt(trading_days_per_year), 8)


def vrp_percentile_rank(current: float, history: Sequence[float]) -> float | None:
    """Percentile of ``current`` within ``history`` values ∈ [0, 100].

    Equal-or-less-than rank; None when history empty.
    """
    if not history:
        return None
    n = len(history)
    finite = [float(h) for h in history if h is not None and math.isfinite(float(h))]
    if not finite:
        return None
    le = sum(1 for v in finite if v <= float(current))
    return round(100.0 * le / n, 4)


def fetch_stock_daily_closes(
    conn: Any,
    symbol: str,
    *,
    end_date: date,
    lookback_days: int,
) -> list[tuple[date, float]]:
    """Ascending list of (bar_date, close) for a symbol up to ``end_date``."""
    sym = symbol.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bar_date, close
            FROM raw_market.stock_daily
            WHERE UPPER(TRIM(symbol)) = %s
              AND bar_date <= %s
            ORDER BY bar_date DESC
            LIMIT %s
            """,
            (sym, end_date, int(lookback_days)),
        )
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []
    cols = ("bar_date", "close")
    out: list[tuple[date, float]] = []
    for r in raw or []:
        d = _row_to_dict(r, cols)
        bd = _as_date(d.get("bar_date"))
        if bd is None:
            continue
        try:
            close = float(d.get("close"))
        except (TypeError, ValueError):
            continue
        if close <= 0 or not math.isfinite(close):
            continue
        out.append((bd, close))
    out.sort(key=lambda x: x[0])
    return out


def fetch_atm_iv_30d(
    conn: Any,
    symbol: str,
    *,
    trade_date: date,
) -> float | None:
    """Median ATM IV for the expiry nearest to 30 DTE on ``trade_date``.

    Falls back to any-expiry median when no expiry lies in the 15–60 day band.
    """
    sym = symbol.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT expiry, atm_iv
            FROM features.option_metric_atm_iv_daily
            WHERE UPPER(TRIM(symbol)) = %s
              AND trade_date = %s
              AND atm_iv IS NOT NULL
            """,
            (sym, trade_date),
        )
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []
    cols = ("expiry", "atm_iv")
    band: list[float] = []
    all_ivs: list[float] = []
    for r in raw or []:
        d = _row_to_dict(r, cols)
        exp = _as_date(d.get("expiry"))
        try:
            iv = float(d.get("atm_iv"))
        except (TypeError, ValueError):
            continue
        if not (0.0 < iv < 10.0):
            continue
        all_ivs.append(iv)
        if exp is None:
            continue
        dte = (exp - trade_date).days
        if 15 <= dte <= 60:
            band.append(iv)
    if band:
        return round(float(median(band)), 8)
    if all_ivs:
        return round(float(median(all_ivs)), 8)
    return None


def fetch_prior_vrp_history(
    conn: Any,
    symbol: str,
    *,
    end_date: date,
    window: int = _TRADING_DAYS_PER_YEAR,
) -> list[float]:
    """Return trailing ``window`` vrp_60d values ending BEFORE ``end_date``."""
    sym = symbol.strip().upper()
    lookback_start = end_date - timedelta(days=window * 2 + 30)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, vrp_60d
            FROM features.stock_signal_vrp_daily
            WHERE UPPER(TRIM(symbol)) = %s
              AND trade_date >= %s
              AND trade_date < %s
              AND vrp_60d IS NOT NULL
            ORDER BY trade_date DESC
            LIMIT %s
            """,
            (sym, lookback_start, end_date, int(window)),
        )
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []
    values: list[float] = []
    for r in raw or []:
        if isinstance(r, Mapping):
            v = r.get("vrp_60d")
        else:
            v = r[1] if len(r) > 1 else None
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            values.append(f)
    return values


def compute_vrp_row(
    *,
    closes: Sequence[float],
    atm_iv_30d: float | None,
    vrp_60d_history: Sequence[float] | None = None,
) -> dict[str, Any] | None:
    """Build one VRP row from an ascending close series + current ATM IV.

    Returns None when even the shortest RV window (20d) cannot be computed.
    """
    rv_20 = annualized_close_to_close_rv(closes, 20)
    if rv_20 is None:
        return None
    rv_60 = annualized_close_to_close_rv(closes, 60)
    rv_252 = annualized_close_to_close_rv(closes, 252)

    vrp_20 = None
    vrp_60 = None
    if atm_iv_30d is not None and math.isfinite(atm_iv_30d):
        vrp_20 = round(float(atm_iv_30d) - rv_20, 8)
        if rv_60 is not None:
            vrp_60 = round(float(atm_iv_30d) - rv_60, 8)

    vrp_pct = None
    if vrp_60 is not None and vrp_60d_history:
        vrp_pct = vrp_percentile_rank(vrp_60, vrp_60d_history)

    return {
        "rv_20d": rv_20,
        "rv_60d": rv_60,
        "rv_252d": rv_252,
        "atm_iv_30d": (
            round(float(atm_iv_30d), 8)
            if atm_iv_30d is not None and math.isfinite(atm_iv_30d)
            else None
        ),
        "vrp_20d": vrp_20,
        "vrp_60d": vrp_60,
        "vrp_pct_252d": vrp_pct,
    }


def compute_vrp_for_symbol(
    conn: Any,
    *,
    symbol: str,
    trade_date: date,
) -> dict[str, Any] | None:
    """Compute one symbol's VRP row for ``trade_date``. Returns the row dict or None."""
    closes_pairs = fetch_stock_daily_closes(
        conn,
        symbol,
        end_date=trade_date,
        lookback_days=280,
    )
    # Guard: last bar must equal trade_date; otherwise the symbol has no data on that day.
    if not closes_pairs or closes_pairs[-1][0] != trade_date:
        return None
    closes = [c for _d, c in closes_pairs]
    atm_iv = fetch_atm_iv_30d(conn, symbol, trade_date=trade_date)
    history = fetch_prior_vrp_history(conn, symbol, end_date=trade_date)
    features = compute_vrp_row(
        closes=closes,
        atm_iv_30d=atm_iv,
        vrp_60d_history=history,
    )
    if features is None:
        return None
    return {"symbol": symbol.strip().upper(), "trade_date": trade_date, **features}


def compute_vrp_for_date(
    conn: Any,
    *,
    trade_date: date,
    underlyings: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Compute VRP for each symbol in ``underlyings`` (or discover via stock_daily)."""
    syms = _resolve_symbols(conn, trade_date, underlyings)
    if not syms:
        return {
            "trade_date": trade_date.isoformat(),
            "symbols": 0,
            "rows_written": 0,
            "skipped": 0,
        }

    now = datetime.now(timezone.utc)
    upserts: list[tuple[Any, ...]] = []
    skipped = 0
    for sym in syms:
        row = compute_vrp_for_symbol(conn, symbol=sym, trade_date=trade_date)
        if row is None:
            skipped += 1
            continue
        upserts.append(
            (
                row["symbol"],
                row["trade_date"],
                row["rv_20d"],
                row["rv_60d"],
                row["rv_252d"],
                row["atm_iv_30d"],
                row["vrp_20d"],
                row["vrp_60d"],
                row["vrp_pct_252d"],
                None,  # fwd_ret_20d backfilled later
                now,
            )
        )

    n = batch_upsert(
        conn,
        "features.stock_signal_vrp_daily",
        _COLS,
        upserts,
        conflict_keys=("symbol", "trade_date"),
        update_cols=(
            "rv_20d",
            "rv_60d",
            "rv_252d",
            "atm_iv_30d",
            "vrp_20d",
            "vrp_60d",
            "vrp_pct_252d",
            "computed_at",
        ),
        set_fetched_at=False,
    )
    return {
        "trade_date": trade_date.isoformat(),
        "symbols": len(syms),
        "rows_written": n,
        "skipped": skipped,
    }


def _resolve_symbols(
    conn: Any,
    trade_date: date,
    underlyings: Sequence[str] | None,
) -> list[str]:
    if underlyings:
        return sorted({str(s).strip().upper() for s in underlyings if str(s).strip()})
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol
            FROM raw_market.stock_daily
            WHERE bar_date = %s
            ORDER BY 1
            LIMIT 2000
            """,
            (trade_date,),
        )
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []
    out: list[str] = []
    for r in raw or []:
        if isinstance(r, Mapping):
            sym = r.get("symbol") or next(iter(r.values()), None)
        else:
            sym = r[0] if r else None
        if sym:
            out.append(str(sym).strip().upper())
    return sorted(set(out))


# Backfill helper: exposed so the fwd_ret_20d job can call it separately.
def compute_fwd_ret_20d(
    closes_pairs: Sequence[tuple[date, float]],
    *,
    trade_date: date,
) -> float | None:
    """Log return between close at ``trade_date`` and 20 trading days later.

    ``closes_pairs`` must be ordered oldest → newest and include the anchor.
    Returns None when 20 subsequent closes are not yet available.
    """
    if not closes_pairs:
        return None
    dates = [d for d, _ in closes_pairs]
    if trade_date not in dates:
        return None
    idx = dates.index(trade_date)
    if idx + 20 >= len(closes_pairs):
        return None
    anchor = closes_pairs[idx][1]
    tail = closes_pairs[idx + 20][1]
    if anchor <= 0 or tail <= 0:
        return None
    try:
        return round(math.log(tail / anchor), 8)
    except (ValueError, ZeroDivisionError):
        return None
