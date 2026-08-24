"""Momentum Radar: multi-timeframe scoring from stock_daily + stock_snapshot.

Factors (0–100 each, then weighted into composite 0–100):

| Factor       | Source                         | Notes |
|--------------|--------------------------------|-------|
| Z_SDT        | stock_daily returns            | Z of 5d return vs 20d std |
| Z_V          | stock_daily volume             | Volume z-score (20d) |
| Accept VWAP  | stock_daily / snapshot VWAP    | Close vs VWAP acceptance |
| Z_OFI        | live Redis OFI (optional)      | Stub → neutral 50 if unavailable |
| H_52w        | stock_daily 252d high          | Close / 52w high |
| O+           | stock_daily open vs prev close | Gap / open strength |
| A            | short vs medium momentum       | Acceleration |
| R_sec        | relative strength              | Stub: vs own 60d return (no sector map) |
| Crash        | drawdown from 20d high         | Crash risk (inverted into score) |

Grades: A+ (>=85) / A (>=75) / B (>=60) / C (>=45) / D (<45)
Paths: EXT / PB / FAIL / HALT

D10 BLOCKED — read-only analytics; no trade execution.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from statistics import mean, pstdev
from typing import Any, Mapping, Sequence

from bifrost_research.db.upsert import batch_upsert

_COLS = (
    "symbol",
    "trade_date",
    "score",
    "grade",
    "path",
    "z_sdt",
    "z_v",
    "accept_vwap",
    "z_ofi",
    "h_52w",
    "o_plus",
    "a_factor",
    "r_sec",
    "crash",
    "factors_json",
    "computed_at",
)

# Composite weights (sum = 1.0)
_WEIGHTS: dict[str, float] = {
    "z_sdt": 0.20,
    "z_v": 0.10,
    "accept_vwap": 0.15,
    "z_ofi": 0.10,
    "h_52w": 0.15,
    "o_plus": 0.10,
    "a_factor": 0.10,
    "r_sec": 0.05,
    "crash": 0.05,
}

FACTOR_NOTES: dict[str, str] = {
    "z_sdt": "Computed from market.stock_daily close returns",
    "z_v": "Computed from market.stock_daily volume",
    "accept_vwap": "Computed from close vs VWAP (daily or snapshot)",
    "z_ofi": "Requires live Redis OFI; stubbed to neutral 50 when unavailable",
    "h_52w": "Computed from 252-session high on stock_daily",
    "o_plus": "Computed from open vs previous close",
    "a_factor": "Acceleration: 5d momentum vs 20d momentum",
    "r_sec": "Sector relative strength unavailable without sector map; uses own 60d return rank proxy",
    "crash": "Drawdown from 20d high (inverted: lower crash risk → higher contribution)",
}


@dataclass(frozen=True)
class DailyBar:
    bar_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _z_to_score(z: float, *, scale: float = 2.0) -> float:
    """Map z-score to 0–100 via logistic-ish clamp around ±scale."""
    # z=+scale → ~84, z=0 → 50, z=-scale → ~16
    return _clamp(50.0 + 25.0 * (z / scale))


def grade_from_score(score: float) -> str:
    if score >= 85:
        return "A+"
    if score >= 75:
        return "A"
    if score >= 60:
        return "B"
    if score >= 45:
        return "C"
    return "D"


def path_from_factors(
    *,
    score: float,
    h_52w: float,
    a_factor: float,
    crash: float,
    accept_vwap: float,
) -> str:
    """Classify path: EXT / PB / FAIL / HALT.

    - HALT: elevated crash risk (crash factor score low = high risk)
    - FAIL: weak score + weak acceptance / negative acceleration
    - EXT: strong score near highs
    - PB: otherwise constructive / pullback regime
    """
    if crash < 35:
        return "HALT"
    if score < 45 and (accept_vwap < 40 or a_factor < 40):
        return "FAIL"
    if score >= 70 and h_52w >= 70:
        return "EXT"
    if score >= 55 and h_52w >= 55:
        return "EXT"
    if score >= 50 and accept_vwap < 55 and h_52w >= 50:
        return "PB"
    if score < 50:
        return "FAIL"
    return "PB"


def _factor_z_sdt(closes: Sequence[float]) -> float:
    """Z of 5d log-return vs trailing 20d return std (approx)."""
    if len(closes) < 26:
        return 50.0
    r5 = math.log(closes[-1] / closes[-6]) if closes[-6] > 0 else 0.0
    rets = []
    for i in range(len(closes) - 20, len(closes)):
        if i <= 0 or closes[i - 1] <= 0 or closes[i] <= 0:
            continue
        rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 5:
        return 50.0
    mu = mean(rets)
    sd = pstdev(rets) if len(rets) > 1 else 0.0
    if sd <= 1e-12:
        return 50.0
    # Scale 5d return to daily-equivalent z roughly
    z = (r5 / math.sqrt(5.0) - mu) / sd
    return _z_to_score(z)


def _factor_z_v(volumes: Sequence[float]) -> float:
    if len(volumes) < 21:
        return 50.0
    window = volumes[-21:-1]
    cur = volumes[-1]
    mu = mean(window)
    sd = pstdev(window) if len(window) > 1 else 0.0
    if sd <= 1e-12:
        return 50.0
    z = (cur - mu) / sd
    return _z_to_score(z, scale=2.5)


def _factor_accept_vwap(close: float, vwap: float | None) -> float:
    if vwap is None or vwap <= 0 or close <= 0:
        return 50.0
    # Soft score: within ±1% of VWAP → mid; above → higher
    pct = (close - vwap) / vwap
    return _clamp(50.0 + pct * 2000.0)  # +1% → 70


def _factor_h_52w(closes: Sequence[float]) -> float:
    window = closes[-252:] if len(closes) >= 20 else closes
    if not window:
        return 50.0
    hi = max(window)
    if hi <= 0:
        return 50.0
    return _clamp(100.0 * closes[-1] / hi)


def _factor_o_plus(open_: float, prev_close: float) -> float:
    if prev_close <= 0 or open_ <= 0:
        return 50.0
    gap = (open_ - prev_close) / prev_close
    return _clamp(50.0 + gap * 2500.0)  # +1% gap → 75


def _factor_acceleration(closes: Sequence[float]) -> float:
    if len(closes) < 21:
        return 50.0
    m5 = closes[-1] / closes[-6] - 1.0 if closes[-6] > 0 else 0.0
    m20 = closes[-1] / closes[-21] - 1.0 if closes[-21] > 0 else 0.0
    # Acceleration positive when short momentum exceeds medium
    diff = m5 - (m20 / 4.0)  # scale 20d to ~5d
    return _clamp(50.0 + diff * 500.0)


def _factor_r_sec(closes: Sequence[float]) -> float:
    """Proxy relative strength: 60d return mapped to 0–100 (no sector peers)."""
    if len(closes) < 61 or closes[-61] <= 0:
        return 50.0
    ret = closes[-1] / closes[-61] - 1.0
    return _clamp(50.0 + ret * 200.0)  # +25% → 100


def _factor_crash(closes: Sequence[float], highs: Sequence[float]) -> float:
    """Higher score = lower crash risk. Based on drawdown from 20d high."""
    if len(closes) < 5 or len(highs) < 5:
        return 50.0
    n = min(20, len(closes), len(highs))
    peak = max(highs[-n:])
    if peak <= 0:
        return 50.0
    dd = (closes[-1] - peak) / peak  # negative when below peak
    # -10% dd → ~25; 0% → 100
    return _clamp(100.0 + dd * 750.0)


def score_momentum(
    bars: Sequence[DailyBar],
    *,
    z_ofi: float | None = None,
    snapshot_vwap: float | None = None,
) -> dict[str, Any]:
    """Pure compute: bars chronological ascending. Returns factor dict + score/grade/path."""
    if len(bars) < 5:
        raise ValueError("Need at least 5 daily bars")

    closes = [b.close for b in bars]
    volumes = [float(b.volume) for b in bars]
    highs = [b.high for b in bars]
    last = bars[-1]
    prev = bars[-2]

    vwap = snapshot_vwap if snapshot_vwap is not None else last.vwap

    factors = {
        "z_sdt": round(_factor_z_sdt(closes), 4),
        "z_v": round(_factor_z_v(volumes), 4),
        "accept_vwap": round(_factor_accept_vwap(last.close, vwap), 4),
        "z_ofi": round(float(z_ofi) if z_ofi is not None else 50.0, 4),
        "h_52w": round(_factor_h_52w(closes), 4),
        "o_plus": round(_factor_o_plus(last.open, prev.close), 4),
        "a_factor": round(_factor_acceleration(closes), 4),
        "r_sec": round(_factor_r_sec(closes), 4),
        "crash": round(_factor_crash(closes, highs), 4),
    }
    score = round(
        sum(factors[k] * _WEIGHTS[k] for k in _WEIGHTS),
        4,
    )
    grade = grade_from_score(score)
    path = path_from_factors(
        score=score,
        h_52w=factors["h_52w"],
        a_factor=factors["a_factor"],
        crash=factors["crash"],
        accept_vwap=factors["accept_vwap"],
    )
    return {
        "score": score,
        "grade": grade,
        "path": path,
        "factors": factors,
        "z_ofi_available": z_ofi is not None,
        "factor_notes": FACTOR_NOTES,
    }


def _row_to_dict(row: Any, columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {columns[i]: row[i] for i in range(min(len(columns), len(row)))}


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


def fetch_daily_bars(
    conn: Any,
    symbol: str,
    *,
    as_of: date,
    lookback: int = 280,
) -> list[DailyBar]:
    """Load ascending stock_daily bars ending at ``as_of``."""
    cols = ("bar_date", "open", "high", "low", "close", "volume", "vwap")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bar_date, open, high, low, close, volume, vwap
            FROM raw_market.stock_daily
            WHERE UPPER(TRIM(symbol)) = %s AND bar_date <= %s
            ORDER BY bar_date DESC
            LIMIT %s
            """,
            (symbol.strip().upper(), as_of, lookback),
        )
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []
    rows = [_row_to_dict(r, cols) for r in (raw or [])]
    bars: list[DailyBar] = []
    for r in reversed(rows):
        bd = _as_date(r.get("bar_date"))
        if bd is None:
            continue
        try:
            bars.append(
                DailyBar(
                    bar_date=bd,
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r.get("volume") or 0),
                    vwap=float(r["vwap"]) if r.get("vwap") is not None else None,
                )
            )
        except (TypeError, ValueError, KeyError):
            continue
    return bars


def fetch_snapshot_vwap(conn: Any, symbol: str, session_date: date) -> float | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT vwap FROM raw_market.stock_snapshot
            WHERE UPPER(TRIM(symbol)) = %s AND session_date = %s
            """,
            (symbol.strip().upper(), session_date),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        v = row.get("vwap")
    else:
        v = row[0]
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_momentum_for_symbol(
    conn: Any,
    *,
    symbol: str,
    trade_date: date,
    z_ofi: float | None = None,
) -> dict[str, Any] | None:
    """Compute + upsert one symbol. Returns result dict or None if insufficient data."""
    bars = fetch_daily_bars(conn, symbol, as_of=trade_date)
    if len(bars) < 5:
        return None
    snap_vwap = fetch_snapshot_vwap(conn, symbol, trade_date)
    result = score_momentum(bars, z_ofi=z_ofi, snapshot_vwap=snap_vwap)
    factors = result["factors"]
    now = datetime.now(timezone.utc)
    row = (
        symbol.strip().upper(),
        trade_date,
        result["score"],
        result["grade"],
        result["path"],
        factors["z_sdt"],
        factors["z_v"],
        factors["accept_vwap"],
        factors["z_ofi"],
        factors["h_52w"],
        factors["o_plus"],
        factors["a_factor"],
        factors["r_sec"],
        factors["crash"],
        {
            "factors": factors,
            "z_ofi_available": result["z_ofi_available"],
            "notes": FACTOR_NOTES,
        },
        now,
    )
    batch_upsert(
        conn,
        "features.stock_signal_momentum_daily",
        _COLS,
        [row],
        conflict_keys=("symbol", "trade_date"),
        update_cols=(
            "score",
            "grade",
            "path",
            "z_sdt",
            "z_v",
            "accept_vwap",
            "z_ofi",
            "h_52w",
            "o_plus",
            "a_factor",
            "r_sec",
            "crash",
            "factors_json",
            "computed_at",
        ),
        set_fetched_at=False,
    )
    return {
        "symbol": symbol.strip().upper(),
        "trade_date": trade_date.isoformat(),
        **result,
    }


def compute_momentum_for_date(
    conn: Any,
    *,
    trade_date: date,
    symbols: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Batch compute for symbols (or all with stock_daily on trade_date)."""
    if symbols:
        syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    else:
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
            raw = cur.fetchall() or []
        syms = []
        for r in raw:
            if isinstance(r, Mapping):
                syms.append(str(next(iter(r.values()))))
            else:
                syms.append(str(r[0]))

    written = 0
    skipped = 0
    for sym in syms:
        out = compute_momentum_for_symbol(conn, symbol=sym, trade_date=trade_date)
        if out is None:
            skipped += 1
        else:
            written += 1
    return {
        "trade_date": trade_date.isoformat(),
        "symbols": len(syms),
        "rows_written": written,
        "skipped": skipped,
    }
