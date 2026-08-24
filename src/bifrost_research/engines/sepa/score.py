"""SEPA fusion scoring engine.

Pure-Python compute + PostgreSQL adapters. Everything here is deterministic and
unit-testable via injected daily-bar / fundamental / options rows so the DB
plumbing can be exercised separately from the scoring logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from bifrost_research.db.upsert import batch_upsert

_COLS = (
    "symbol",
    "trade_date",
    "fundamental_score",
    "trend_template_score",
    "momentum_score",
    "structure_score",
    "sepa_score",
    "grade",
    "stage",
    "path",
    "trend_template_pass",
    "fundamental_pass",
    "latest_close",
    "sma_50",
    "sma_150",
    "sma_200",
    "high_52w",
    "low_52w",
    "iv_percentile",
    "pcr_oi",
    "fund_pass_count",
    "tech_pass_count",
    "factors_json",
    "computed_at",
)

# Composite weights (must sum to 1.0)
WEIGHTS: dict[str, float] = {
    "fundamental": 0.30,
    "trend_template": 0.35,
    "momentum": 0.20,
    "structure": 0.15,
}

# Minervini fundamental condition columns (from dw_stock.mart_sepa_fundamental_eval)
FUND_CONDITION_COLUMNS = (
    "eps_q2q_ge_25pct",
    "rev_q2q_ge_25pct",
    "eps_acc_2q",
    "rev_acc_2q",
    "eps_3y_ge_15pct",
    "rev_3y_ge_15pct",
    "eps_acc_fy",
    "rev_acc_fy",
)


# ---------------------------------------------------------------------------
# Pure-compute layer
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DailyBar:
    bar_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _sma(values: Sequence[float], window: int) -> float | None:
    if len(values) < window:
        return None
    tail = values[-window:]
    return sum(tail) / window


def _grade_from_score(score: float) -> str:
    if score >= 85:
        return "A+"
    if score >= 75:
        return "A"
    if score >= 60:
        return "B"
    if score >= 45:
        return "C"
    return "D"


def compute_trend_template(bars: Sequence[DailyBar]) -> dict[str, Any]:
    """Evaluate Minervini's 8 Trend Template criteria + derived score/stage.

    Requires at least ~252 bars for the 52-week window. Returns a fully-populated
    dict even when input is shorter (missing criteria mark as False).
    """
    closes = [b.close for b in bars]
    if not closes:
        return {
            "trend_template_score": 0.0,
            "trend_template_pass": False,
            "trend_template_pass_count": 0,
            "criteria": {},
            "latest_close": None,
            "sma_50": None,
            "sma_150": None,
            "sma_200": None,
            "high_52w": None,
            "low_52w": None,
        }

    last_close = closes[-1]
    sma_50 = _sma(closes, 50)
    sma_150 = _sma(closes, 150)
    sma_200 = _sma(closes, 200)
    # SMA-200 trend (last vs. ~20 bars ago)
    sma_200_20d_ago = None
    if len(closes) >= 220:
        sma_200_20d_ago = sum(closes[-220:-20]) / 200
    window_252 = closes[-252:] if len(closes) >= 252 else closes
    high_52w = max(window_252) if window_252 else None
    low_52w = min(window_252) if window_252 else None

    def pct(x: float | None, y: float | None) -> float | None:
        if x is None or y is None or y <= 0:
            return None
        return x / y

    c: dict[str, bool] = {}
    c["price_gt_sma50"] = bool(sma_50 and last_close > sma_50)
    c["price_gt_sma150"] = bool(sma_150 and last_close > sma_150)
    c["price_gt_sma200"] = bool(sma_200 and last_close > sma_200)
    c["sma50_gt_sma150"] = bool(sma_50 and sma_150 and sma_50 > sma_150)
    c["sma50_gt_sma200"] = bool(sma_50 and sma_200 and sma_50 > sma_200)
    c["sma150_gt_sma200"] = bool(sma_150 and sma_200 and sma_150 > sma_200)
    c["sma200_rising_1m"] = bool(
        sma_200 and sma_200_20d_ago and sma_200 > sma_200_20d_ago
    )
    ratio_low = pct(last_close, low_52w)
    ratio_high = pct(last_close, high_52w)
    c["close_ge_low52_x_1_3"] = bool(ratio_low is not None and ratio_low >= 1.3)
    c["close_ge_high52_x_0_75"] = bool(ratio_high is not None and ratio_high >= 0.75)

    pass_count = sum(1 for v in c.values() if v)
    total = len(c)  # 9 criteria (Minervini's 8 + close-ge-low52 pair split)
    score = round(100.0 * pass_count / total, 4)

    return {
        "trend_template_score": score,
        "trend_template_pass": pass_count == total,
        "trend_template_pass_count": pass_count,
        "criteria": c,
        "latest_close": last_close,
        "sma_50": sma_50,
        "sma_150": sma_150,
        "sma_200": sma_200,
        "high_52w": high_52w,
        "low_52w": low_52w,
    }


def _fundamental_score_from_eval(eval_row: Mapping[str, Any] | None) -> dict[str, Any]:
    """Convert dbt ``mart_sepa_fundamental_eval`` row into F sub-score.

    Missing / insufficient data → neutral 50 with ``insufficient=True``.
    """
    if not eval_row:
        return {
            "fundamental_score": 50.0,
            "fundamental_pass": False,
            "fund_pass_count": 0,
            "insufficient": True,
            "criteria": {},
        }
    if eval_row.get("insufficient_data"):
        return {
            "fundamental_score": 50.0,
            "fundamental_pass": False,
            "fund_pass_count": int(eval_row.get("pass_count") or 0),
            "insufficient": True,
            "criteria": {k: bool(eval_row.get(k)) for k in FUND_CONDITION_COLUMNS},
        }
    pass_count = 0
    criteria: dict[str, bool] = {}
    for col in FUND_CONDITION_COLUMNS:
        v = bool(eval_row.get(col))
        criteria[col] = v
        if v:
            pass_count += 1
    total = len(FUND_CONDITION_COLUMNS)
    return {
        "fundamental_score": round(100.0 * pass_count / total, 4),
        "fundamental_pass": pass_count == total,
        "fund_pass_count": pass_count,
        "insufficient": False,
        "criteria": criteria,
    }


def _structure_score(
    *,
    iv_percentile: float | None,
    pcr_oi: float | None,
    spot: float | None,
    zero_gamma: float | None,
    call_wall: float | None,
    put_wall: float | None,
) -> dict[str, Any]:
    """Options Structure score 0–100.

    Higher = more favorable long-side entry structure. Composed of:

    * IV percentile (low = cheap options, favors long premium) → invert.
    * PCR OI: healthy contrarian bull ≈ 0.7–1.0; extreme > 1.3 or < 0.4 penalized.
    * Spot vs GEX walls: near put-wall support = neutral; near call-wall
      resistance = mildly negative; above zero-gamma line (positive dealer
      gamma) = supportive.
    """
    sub: dict[str, float] = {}
    parts_available = 0

    # IV percentile: 0-100. Invert (low IV good for long premium entries).
    if iv_percentile is not None:
        sub["iv_pct_component"] = _clamp(100.0 - float(iv_percentile))
        parts_available += 1

    # PCR OI: bell-shaped around 0.85 (mild put skew is healthy for continuation).
    if pcr_oi is not None:
        p = float(pcr_oi)
        # Distance from 0.85 penalized quadratically; ±0.5 range → 0.
        dist = abs(p - 0.85)
        sub["pcr_component"] = _clamp(100.0 - min(1.0, dist / 0.5) * 100.0)
        parts_available += 1

    # GEX / spot relationship.
    if spot is not None and zero_gamma is not None:
        zg = float(zero_gamma)
        # +1% above zero-gamma → +100; -1% below → 0; linear between.
        if zg > 0:
            offset_pct = (spot - zg) / zg
            sub["zero_gamma_component"] = _clamp(50.0 + offset_pct * 5000.0)
            parts_available += 1

    if spot is not None and call_wall is not None and put_wall is not None:
        cw = float(call_wall)
        pw = float(put_wall)
        if cw > pw > 0:
            band = cw - pw
            # Position within band: 0 = at put wall (support), 1 = at call wall (cap).
            pos = (spot - pw) / band if band > 0 else 0.5
            # Mid-band ≈ 65 (best breathing room). At walls ≈ 35.
            sub["wall_component"] = _clamp(65.0 - abs(pos - 0.5) * 60.0)
            parts_available += 1

    if parts_available == 0:
        return {"structure_score": 50.0, "available_parts": 0, "components": {}}

    score = sum(sub.values()) / parts_available
    return {
        "structure_score": round(score, 4),
        "available_parts": parts_available,
        "components": {k: round(v, 4) for k, v in sub.items()},
    }


def _classify_stage(
    *,
    trend: dict[str, Any],
    momentum_score: float,
) -> str:
    """Weinstein-style stage classification driven by trend template + momentum."""
    c = trend.get("criteria") or {}
    last = trend.get("latest_close")
    high_52w = trend.get("high_52w")
    low_52w = trend.get("low_52w")

    if not c:
        return "STAGE_1"

    sma_stack = (
        c.get("price_gt_sma50")
        and c.get("price_gt_sma150")
        and c.get("price_gt_sma200")
        and c.get("sma50_gt_sma150")
        and c.get("sma150_gt_sma200")
    )
    sma200_rising = c.get("sma200_rising_1m")
    near_high = (
        last is not None
        and high_52w is not None
        and high_52w > 0
        and last / high_52w >= 0.92
    )
    ratio_low = None
    if last is not None and low_52w is not None and low_52w > 0:
        ratio_low = last / low_52w

    # Stage 4: below SMA200 (distribution / decline)
    if last is not None and trend.get("sma_200") and last < trend["sma_200"]:
        return "STAGE_4"
    # Stage 3: extended top — near high but momentum decelerating
    if near_high and momentum_score < 55 and sma_stack:
        return "STAGE_3"
    # Stage 2C: climax (near high, hot momentum)
    if near_high and momentum_score >= 75 and sma_stack:
        return "STAGE_2C"
    # Stage 2B: strong advance
    if sma_stack and sma200_rising and momentum_score >= 60:
        return "STAGE_2B"
    # Stage 2A: base breakout (partial stack + rising momentum)
    if (
        c.get("price_gt_sma50")
        and c.get("price_gt_sma150")
        and momentum_score >= 55
    ):
        return "STAGE_2A"
    # Stage 1: base / accumulation
    if ratio_low is not None and ratio_low >= 1.0:
        return "STAGE_1"
    return "STAGE_1"


def _classify_path(*, stage: str, sepa_score: float, structure: dict[str, Any]) -> str:
    """Map (stage, sepa_score) → actionable path."""
    if stage == "STAGE_4":
        return "AVOID"
    if stage == "STAGE_3":
        return "WATCH"
    if stage == "STAGE_2C":
        return "EXTENDED"
    if stage in ("STAGE_2A", "STAGE_2B"):
        if sepa_score >= 70:
            return "PIVOT"
        if sepa_score >= 55:
            return "SETUP"
        return "WATCH"
    return "WATCH"


def fuse_sepa(
    *,
    trend: dict[str, Any],
    fundamental: dict[str, Any],
    momentum_score: float,
    structure: dict[str, Any],
) -> dict[str, Any]:
    """Fuse the 4 sub-scores into composite + stage + path."""
    f = float(fundamental.get("fundamental_score") or 50.0)
    t = float(trend.get("trend_template_score") or 0.0)
    m = float(momentum_score if momentum_score is not None else 50.0)
    o = float(structure.get("structure_score") or 50.0)

    composite = round(
        WEIGHTS["fundamental"] * f
        + WEIGHTS["trend_template"] * t
        + WEIGHTS["momentum"] * m
        + WEIGHTS["structure"] * o,
        4,
    )
    grade = _grade_from_score(composite)
    stage = _classify_stage(trend=trend, momentum_score=m)
    path = _classify_path(stage=stage, sepa_score=composite, structure=structure)
    return {
        "sepa_score": composite,
        "grade": grade,
        "stage": stage,
        "path": path,
        "weights": dict(WEIGHTS),
    }


# ---------------------------------------------------------------------------
# DB adapters
# ---------------------------------------------------------------------------

def fetch_daily_bars(
    conn: Any,
    symbol: str,
    *,
    as_of: date,
    lookback: int = 280,
) -> list[DailyBar]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bar_date, open, high, low, close, volume
            FROM raw_market.stock_daily
            WHERE UPPER(TRIM(symbol)) = %s AND bar_date <= %s
            ORDER BY bar_date DESC
            LIMIT %s
            """,
            (symbol.strip().upper(), as_of, lookback),
        )
        raw = cur.fetchall() if hasattr(cur, "fetchall") else []
    bars: list[DailyBar] = []
    for row in reversed(list(raw or [])):
        if isinstance(row, Mapping):
            bd = row.get("bar_date")
            values = (
                row.get("open"),
                row.get("high"),
                row.get("low"),
                row.get("close"),
                row.get("volume"),
            )
        else:
            bd = row[0]
            values = (row[1], row[2], row[3], row[4], row[5])
        if bd is None:
            continue
        if isinstance(bd, datetime):
            bd = bd.date()
        try:
            bars.append(
                DailyBar(
                    bar_date=bd,
                    open=float(values[0]),
                    high=float(values[1]),
                    low=float(values[2]),
                    close=float(values[3]),
                    volume=float(values[4] or 0),
                )
            )
        except (TypeError, ValueError):
            continue
    return bars


def fetch_fundamental_eval(
    conn: Any, symbol: str, *, as_of: date
) -> Mapping[str, Any] | None:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT eps_q2q_ge_25pct, rev_q2q_ge_25pct, eps_acc_2q, rev_acc_2q,
                       eps_3y_ge_15pct, rev_3y_ge_15pct, eps_acc_fy, rev_acc_fy,
                       insufficient_data, pass_count
                FROM dw_stock.mart_sepa_fundamental_eval
                WHERE symbol = %s AND eval_date <= %s
                ORDER BY eval_date DESC
                LIMIT 1
                """,
                (symbol.strip().upper(), as_of),
            )
            row = cur.fetchone()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return None
    if row is None:
        return None
    cols = (
        "eps_q2q_ge_25pct",
        "rev_q2q_ge_25pct",
        "eps_acc_2q",
        "rev_acc_2q",
        "eps_3y_ge_15pct",
        "rev_3y_ge_15pct",
        "eps_acc_fy",
        "rev_acc_fy",
        "insufficient_data",
        "pass_count",
    )
    if isinstance(row, Mapping):
        return dict(row)
    return {cols[i]: row[i] for i in range(min(len(cols), len(row)))}


def fetch_momentum_score(conn: Any, symbol: str, *, as_of: date) -> float | None:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT score
                FROM features_signals.momentum_score_daily
                WHERE symbol = %s AND trade_date <= %s
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                (symbol.strip().upper(), as_of),
            )
            row = cur.fetchone()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return None
    if row is None:
        return None
    val = row.get("score") if isinstance(row, Mapping) else row[0]
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def fetch_options_context(
    conn: Any, symbol: str, *, as_of: date
) -> dict[str, Any]:
    """Fetch IV percentile / PCR / GEX walls best-effort. Any missing piece → None."""
    out: dict[str, Any] = {
        "iv_percentile": None,
        "pcr_oi": None,
        "spot": None,
        "zero_gamma": None,
        "call_wall": None,
        "put_wall": None,
    }
    sym = symbol.strip().upper()
    with conn.cursor() as cur:
        for query, key_map in (
            (
                """
                SELECT iv_percentile_1y FROM features_daily.iv_percentile_daily
                WHERE symbol = %s AND trade_date <= %s
                ORDER BY trade_date DESC LIMIT 1
                """,
                (("iv_percentile",),),
            ),
            (
                """
                SELECT pcr_oi FROM features_daily.pcr_daily
                WHERE symbol = %s AND trade_date <= %s
                ORDER BY trade_date DESC LIMIT 1
                """,
                (("pcr_oi",),),
            ),
            (
                """
                SELECT spot, zero_gamma, major_call_wall, major_put_wall
                FROM features_option.gex_levels_daily
                WHERE symbol = %s AND trade_date <= %s
                ORDER BY trade_date DESC, expiry ASC LIMIT 1
                """,
                (
                    ("spot",),
                    ("zero_gamma",),
                    ("call_wall",),
                    ("put_wall",),
                ),
            ),
        ):
            try:
                cur.execute(query, (sym, as_of))
                row = cur.fetchone()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                continue
            if not row:
                continue
            values = list(row.values()) if isinstance(row, Mapping) else list(row)
            for idx, (key,) in enumerate(key_map):
                if idx >= len(values):
                    break
                v = values[idx]
                if v is None:
                    continue
                try:
                    out[key] = float(v)
                except (TypeError, ValueError):
                    continue
    return out


def compute_sepa_for_symbol(
    conn: Any, *, symbol: str, trade_date: date
) -> dict[str, Any] | None:
    """Compute + upsert one symbol's SEPA score. Returns result dict."""
    bars = fetch_daily_bars(conn, symbol, as_of=trade_date)
    if len(bars) < 50:
        return None

    trend = compute_trend_template(bars)
    fund_row = fetch_fundamental_eval(conn, symbol, as_of=trade_date)
    fund = _fundamental_score_from_eval(fund_row)
    momentum = fetch_momentum_score(conn, symbol, as_of=trade_date)
    momentum_val = momentum if momentum is not None else 50.0
    opts_ctx = fetch_options_context(conn, symbol, as_of=trade_date)
    structure = _structure_score(**opts_ctx)
    fused = fuse_sepa(
        trend=trend,
        fundamental=fund,
        momentum_score=momentum_val,
        structure=structure,
    )

    now = datetime.now(timezone.utc)
    row_values = (
        symbol.strip().upper(),
        trade_date,
        fund["fundamental_score"],
        trend["trend_template_score"],
        momentum_val,
        structure["structure_score"],
        fused["sepa_score"],
        fused["grade"],
        fused["stage"],
        fused["path"],
        bool(trend["trend_template_pass"]),
        bool(fund["fundamental_pass"]),
        trend["latest_close"],
        trend["sma_50"],
        trend["sma_150"],
        trend["sma_200"],
        trend["high_52w"],
        trend["low_52w"],
        opts_ctx.get("iv_percentile"),
        opts_ctx.get("pcr_oi"),
        int(fund["fund_pass_count"]),
        int(trend["trend_template_pass_count"]),
        {
            "trend": {
                "criteria": trend["criteria"],
                "pass_count": trend["trend_template_pass_count"],
            },
            "fundamental": {
                "criteria": fund["criteria"],
                "insufficient": fund["insufficient"],
            },
            "momentum": {
                "score": momentum_val,
                "source": (
                    "features_signals.momentum_score_daily"
                    if momentum is not None
                    else "neutral_fallback"
                ),
            },
            "structure": structure,
            "weights": fused["weights"],
        },
        now,
    )
    batch_upsert(
        conn,
        "features_signals.sepa_score_daily",
        _COLS,
        [row_values],
        conflict_keys=("symbol", "trade_date"),
        update_cols=[c for c in _COLS if c not in ("symbol", "trade_date")],
        set_fetched_at=False,
    )
    return {
        "symbol": symbol.strip().upper(),
        "trade_date": trade_date.isoformat(),
        "fundamental_score": fund["fundamental_score"],
        "trend_template_score": trend["trend_template_score"],
        "momentum_score": momentum_val,
        "structure_score": structure["structure_score"],
        **fused,
    }


def compute_sepa_for_date(
    conn: Any,
    *,
    trade_date: date,
    symbols: Sequence[str],
) -> dict[str, Any]:
    written = 0
    skipped = 0
    for sym in symbols:
        out = compute_sepa_for_symbol(conn, symbol=sym, trade_date=trade_date)
        if out is None:
            skipped += 1
        else:
            written += 1
    return {
        "trade_date": trade_date.isoformat(),
        "symbols": len(symbols),
        "rows_written": written,
        "skipped": skipped,
    }
