"""Pure-compute tests for SEPA fusion engine (no DB)."""

from __future__ import annotations

from datetime import date, timedelta

from bifrost_research.engines.sepa.score import (
    DailyBar,
    FUND_CONDITION_COLUMNS,
    _classify_path,
    _classify_stage,
    _fundamental_score_from_eval,
    _structure_score,
    compute_trend_template,
    fuse_sepa,
)


def _uptrend_bars(n: int = 260, *, start: float = 50.0, drift: float = 0.004) -> list[DailyBar]:
    out: list[DailyBar] = []
    px = start
    d0 = date(2024, 1, 2)
    for i in range(n):
        o = px
        c = px * (1 + drift)
        h = max(o, c) * 1.01
        l = min(o, c) * 0.99
        out.append(
            DailyBar(
                bar_date=d0 + timedelta(days=i),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=1_000_000,
            )
        )
        px = c
    return out


def _downtrend_bars(n: int = 260, *, start: float = 100.0) -> list[DailyBar]:
    out: list[DailyBar] = []
    px = start
    d0 = date(2024, 1, 2)
    for i in range(n):
        o = px
        c = px * (1 - 0.003)
        h = max(o, c) * 1.005
        l = min(o, c) * 0.99
        out.append(
            DailyBar(
                bar_date=d0 + timedelta(days=i),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=1_000_000,
            )
        )
        px = c
    return out


# -----------------------------
# Trend Template
# -----------------------------

def test_trend_template_uptrend_full_pass() -> None:
    trend = compute_trend_template(_uptrend_bars(260, start=30.0, drift=0.005))
    assert trend["trend_template_pass"] is True
    assert trend["trend_template_score"] >= 99.0
    assert trend["latest_close"] > trend["sma_200"]
    assert trend["high_52w"] >= trend["latest_close"]


def test_trend_template_downtrend_low_score() -> None:
    trend = compute_trend_template(_downtrend_bars(260, start=100.0))
    assert trend["trend_template_pass"] is False
    assert trend["trend_template_score"] < 50.0
    assert trend["latest_close"] < trend["sma_200"]


def test_trend_template_empty_bars_safe() -> None:
    trend = compute_trend_template([])
    assert trend["trend_template_pass"] is False
    assert trend["trend_template_score"] == 0.0
    assert trend["criteria"] == {}


# -----------------------------
# Fundamental
# -----------------------------

def test_fundamental_all_pass() -> None:
    eval_row = {c: True for c in FUND_CONDITION_COLUMNS}
    eval_row["insufficient_data"] = False
    eval_row["pass_count"] = 8
    out = _fundamental_score_from_eval(eval_row)
    assert out["fundamental_pass"] is True
    assert out["fundamental_score"] == 100.0
    assert out["insufficient"] is False


def test_fundamental_none_returns_neutral() -> None:
    out = _fundamental_score_from_eval(None)
    assert out["fundamental_score"] == 50.0
    assert out["insufficient"] is True


def test_fundamental_insufficient_data() -> None:
    row = {c: False for c in FUND_CONDITION_COLUMNS}
    row["insufficient_data"] = True
    row["pass_count"] = 0
    out = _fundamental_score_from_eval(row)
    assert out["insufficient"] is True
    assert out["fundamental_score"] == 50.0


# -----------------------------
# Options Structure
# -----------------------------

def test_structure_score_low_iv_pcr_ok() -> None:
    out = _structure_score(
        iv_percentile=20.0,
        pcr_oi=0.85,
        spot=100.0,
        zero_gamma=99.0,
        call_wall=105.0,
        put_wall=95.0,
    )
    assert out["available_parts"] == 4
    assert out["structure_score"] > 60.0


def test_structure_score_missing_all_defaults_50() -> None:
    out = _structure_score(
        iv_percentile=None,
        pcr_oi=None,
        spot=None,
        zero_gamma=None,
        call_wall=None,
        put_wall=None,
    )
    assert out["structure_score"] == 50.0
    assert out["available_parts"] == 0


def test_structure_score_high_iv_penalty() -> None:
    out_low = _structure_score(
        iv_percentile=10.0,
        pcr_oi=None,
        spot=None,
        zero_gamma=None,
        call_wall=None,
        put_wall=None,
    )
    out_high = _structure_score(
        iv_percentile=90.0,
        pcr_oi=None,
        spot=None,
        zero_gamma=None,
        call_wall=None,
        put_wall=None,
    )
    assert out_low["structure_score"] > out_high["structure_score"]


# -----------------------------
# Stage classification
# -----------------------------

def test_stage_2b_strong_uptrend() -> None:
    trend = compute_trend_template(_uptrend_bars(260, start=30.0, drift=0.005))
    stage = _classify_stage(trend=trend, momentum_score=72.0)
    assert stage in {"STAGE_2A", "STAGE_2B", "STAGE_2C"}


def test_stage_4_below_sma200() -> None:
    trend = compute_trend_template(_downtrend_bars(260, start=100.0))
    stage = _classify_stage(trend=trend, momentum_score=30.0)
    assert stage == "STAGE_4"


def test_path_avoid_on_stage4() -> None:
    trend = compute_trend_template(_downtrend_bars(260, start=100.0))
    fund = _fundamental_score_from_eval({c: True for c in FUND_CONDITION_COLUMNS})
    fused = fuse_sepa(
        trend=trend,
        fundamental=fund,
        momentum_score=25.0,
        structure=_structure_score(iv_percentile=50.0, pcr_oi=None, spot=None, zero_gamma=None, call_wall=None, put_wall=None),
    )
    assert fused["stage"] == "STAGE_4"
    assert fused["path"] == "AVOID"


def test_path_setup_or_pivot_on_healthy_uptrend() -> None:
    trend = compute_trend_template(_uptrend_bars(260, start=30.0, drift=0.005))
    fund_row = {c: True for c in FUND_CONDITION_COLUMNS}
    fund_row["insufficient_data"] = False
    fund_row["pass_count"] = 8
    fund = _fundamental_score_from_eval(fund_row)
    structure = _structure_score(
        iv_percentile=25.0,
        pcr_oi=0.85,
        spot=trend["latest_close"],
        zero_gamma=trend["latest_close"] * 0.99,
        call_wall=trend["latest_close"] * 1.03,
        put_wall=trend["latest_close"] * 0.97,
    )
    fused = fuse_sepa(
        trend=trend,
        fundamental=fund,
        momentum_score=72.0,
        structure=structure,
    )
    assert fused["path"] in {"SETUP", "PIVOT", "EXTENDED"}
    assert fused["sepa_score"] >= 70.0
    assert fused["grade"] in {"A+", "A", "B"}


def test_fuse_weights_sum_to_one() -> None:
    from bifrost_research.engines.sepa.score import WEIGHTS

    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_classify_path_extended_stage2c() -> None:
    path = _classify_path(
        stage="STAGE_2C",
        sepa_score=88.0,
        structure={"structure_score": 60.0},
    )
    assert path == "EXTENDED"


def test_classify_path_watch_stage3() -> None:
    path = _classify_path(
        stage="STAGE_3",
        sepa_score=70.0,
        structure={"structure_score": 55.0},
    )
    assert path == "WATCH"
