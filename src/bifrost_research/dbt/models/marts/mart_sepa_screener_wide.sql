{{ config(materialized='table') }}

select
    r.symbol,
    r.eval_date,
    r.overall_rank,
    r.decile,
    r.percentile,
    r.composite_score,
    r.fund_pass_count,
    r.tech_pass_count,
    r.combined_pass_count,
    r.fund_insufficient,

    -- All fundamental conditions
    f.eps_q2q_ge_25pct,
    f.rev_q2q_ge_25pct,
    f.eps_acc_2q,
    f.rev_acc_2q,
    f.eps_3y_ge_15pct,
    f.rev_3y_ge_15pct,
    f.eps_acc_fy,
    f.rev_acc_fy,

    -- All technical conditions
    t.avg_volume_50_gt_threshold,
    t.close_ge_low52_x_1_3,
    t.close_ge_high52_x_0_75,
    t.sma50_gt_sma150,
    t.sma50_gt_sma200,
    t.sma150_gt_sma200,
    t.sma200_rising_1m,
    t.price_gt_sma50,
    t.price_gt_sma150,
    t.price_gt_sma200,
    t.crs_ge_70,

    -- Raw metrics for inspector
    f.eps_q0,
    f.eps_g0,
    f.rev_q0,
    f.rev_g0,
    t.close as latest_close,
    t.sma_50,
    t.sma_150,
    t.sma_200,
    t.volume_ma_50,
    t.crs_percentile,
    t.return_252d,

    -- Tier scores
    r.momentum_score,
    r.structure_score,
    r.sentiment_score,

    -- Universe info
    u.name as company_name,
    u.primary_exchange

from {{ ref('mart_sepa_screening_ranked') }} r
inner join {{ ref('mart_sepa_fundamental_eval') }} f using (symbol)
inner join {{ ref('mart_sepa_technical_eval') }} t using (symbol)
inner join {{ ref('dim_universe') }} u using (symbol)
