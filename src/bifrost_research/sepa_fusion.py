"""SEPA fusion pure-compute helpers (Wave 12).

dbt owns SEPA business logic; this module retains deterministic fusion math for
unit tests and shadow diff reports. No DB writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

# Composite weights (must sum to 1.0) — aligned with dbt mart_sepa_composite_score
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
    """Evaluate Minervini Trend Template criteria + derived score."""
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
    total = len(c)
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
    sub: dict[str, float] = {}
    parts_available = 0

    if iv_percentile is not None:
        sub["iv_pct_component"] = _clamp(100.0 - float(iv_percentile))
        parts_available += 1

    if pcr_oi is not None:
        p = float(pcr_oi)
        dist = abs(p - 0.85)
        sub["pcr_component"] = _clamp(100.0 - min(1.0, dist / 0.5) * 100.0)
        parts_available += 1

    if spot is not None and zero_gamma is not None:
        zg = float(zero_gamma)
        if zg > 0:
            offset_pct = (spot - zg) / zg
            sub["zero_gamma_component"] = _clamp(50.0 + offset_pct * 5000.0)
            parts_available += 1

    if spot is not None and call_wall is not None and put_wall is not None:
        cw = float(call_wall)
        pw = float(put_wall)
        if cw > pw > 0:
            band = cw - pw
            pos = (spot - pw) / band if band > 0 else 0.5
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

    if last is not None and trend.get("sma_200") and last < trend["sma_200"]:
        return "STAGE_4"
    if near_high and momentum_score < 55 and sma_stack:
        return "STAGE_3"
    if near_high and momentum_score >= 75 and sma_stack:
        return "STAGE_2C"
    if sma_stack and sma200_rising and momentum_score >= 60:
        return "STAGE_2B"
    if c.get("price_gt_sma50") and c.get("price_gt_sma150") and momentum_score >= 55:
        return "STAGE_2A"
    if ratio_low is not None and ratio_low >= 1.0:
        return "STAGE_1"
    return "STAGE_1"


def _classify_path(*, stage: str, sepa_score: float, structure: dict[str, Any]) -> str:
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


def dbt_composite_score_0_1(
    *,
    fund_pass_count: int,
    tech_pass_count: int,
    momentum_score_0_1: float,
    options_structure_score_0_1: float,
) -> float:
    """Mirror dbt mart_sepa_composite_score weighted formula (0–1 scale)."""
    return round(
        fund_pass_count / 8.0 * WEIGHTS["fundamental"]
        + tech_pass_count / 11.0 * WEIGHTS["trend_template"]
        + momentum_score_0_1 * WEIGHTS["momentum"]
        + options_structure_score_0_1 * WEIGHTS["structure"],
        4,
    )
