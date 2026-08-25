"""Wave 12 shadow diff: dbt composite formula vs sepa_fusion fuse weights."""

from __future__ import annotations

from bifrost_research.sepa_fusion import (
    dbt_composite_score_0_1,
    fuse_sepa,
    _fundamental_score_from_eval,
    _structure_score,
    FUND_CONDITION_COLUMNS,
    WEIGHTS,
)


def test_dbt_composite_matches_fusion_reference() -> None:
    fund_row = {c: True for c in FUND_CONDITION_COLUMNS}
    fund_row["insufficient_data"] = False
    fund_row["pass_count"] = 8
    fund = _fundamental_score_from_eval(fund_row)

    trend = {
        "trend_template_score": 80.0,
        "criteria": {},
        "latest_close": 100.0,
        "sma_200": 90.0,
        "high_52w": 105.0,
        "low_52w": 80.0,
    }
    momentum_0_1 = 0.72
    structure = _structure_score(
        iv_percentile=25.0,
        pcr_oi=0.85,
        spot=100.0,
        zero_gamma=99.0,
        call_wall=105.0,
        put_wall=95.0,
    )
    options_0_1 = structure["structure_score"] / 100.0

    dbt_score = dbt_composite_score_0_1(
        fund_pass_count=8,
        tech_pass_count=9,
        momentum_score_0_1=momentum_0_1,
        options_structure_score_0_1=options_0_1,
    )
    fused = fuse_sepa(
        trend=trend,
        fundamental=fund,
        momentum_score=momentum_0_1 * 100.0,
        structure=structure,
    )
  # fusion uses 0-100 sub-scores; dbt uses pass-count fractions for F/T
    fusion_from_dbt_inputs = round(
        WEIGHTS["fundamental"] * (8 / 8 * 100)
        + WEIGHTS["trend_template"] * (9 / 11 * 100)
        + WEIGHTS["momentum"] * (momentum_0_1 * 100)
        + WEIGHTS["structure"] * structure["structure_score"],
        4,
    )
    assert abs(dbt_score * 100 - fusion_from_dbt_inputs) < 0.05
    assert fused["sepa_score"] >= 70.0


def test_shadow_report_weights_documented() -> None:
    report = {
        "weights": WEIGHTS,
        "owner": "dbt mart_sepa_composite_score + sepa_projection",
        "pit": "asof_ts = last projection timestamp (daily UPSERT)",
    }
    assert sum(report["weights"].values()) == 1.0
    assert report["owner"].startswith("dbt")
