{{ config(materialized='table') }}

-- Projection mart: stable column subset for features.stock_signal_sepa_daily.
-- SEPA business logic owner = dbt (30F/35T/20M/15O composite).

with base as (
    select
        w.symbol,
        w.eval_date as trade_date,
        c.fund_pass_count,
        c.tech_pass_count,
        c.momentum_score,
        c.structure_score,
        c.composite_score,
        w.latest_close,
        w.sma_50,
        w.sma_150,
        w.sma_200,
        t.low_52w,
        t.high_52w,
        o.iv_percentile,
        o.pcr_oi
    from {{ ref('mart_sepa_screener_wide') }} w
    inner join {{ ref('mart_sepa_composite_score') }} c using (symbol)
    inner join {{ ref('mart_sepa_technical_eval') }} t using (symbol)
    left join {{ ref('mart_sepa_tier_options') }} o using (symbol)
)

select
    symbol,
    trade_date,
    round(fund_pass_count * 100.0 / 8.0, 4) as fundamental_score,
    round(tech_pass_count * 100.0 / 11.0, 4) as trend_template_score,
    round(coalesce(momentum_score, 0)::numeric * 100.0, 4) as momentum_score,
    round(coalesce(structure_score, 0)::numeric * 100.0, 4) as structure_score,
    round(composite_score::numeric * 100.0, 4) as sepa_score,
    case
        when composite_score >= 0.85 then 'A+'
        when composite_score >= 0.75 then 'A'
        when composite_score >= 0.60 then 'B'
        when composite_score >= 0.45 then 'C'
        else 'D'
    end as grade,
    case
        when tech_pass_count >= 8 and composite_score >= 0.70 then 'STAGE_2A'
        when tech_pass_count >= 6 then 'STAGE_2B'
        when composite_score >= 0.55 then 'STAGE_1'
        else 'STAGE_4'
    end as stage,
    case
        when composite_score >= 0.75 and tech_pass_count >= 8 then 'PIVOT'
        when composite_score >= 0.60 then 'SETUP'
        when composite_score >= 0.45 then 'WATCH'
        else 'AVOID'
    end as path,
    (tech_pass_count >= 8) as trend_template_pass,
    (fund_pass_count >= 6) as fundamental_pass,
    latest_close,
    sma_50,
    sma_150,
    sma_200,
    high_52w,
    low_52w,
    iv_percentile,
    pcr_oi,
    fund_pass_count,
    tech_pass_count,
    jsonb_build_object(
        'composite_score', composite_score,
        'momentum_score', momentum_score,
        'structure_score', structure_score,
        'weights', jsonb_build_object(
            'fundamental', 0.30,
            'trend_template', 0.35,
            'momentum', 0.20,
            'structure', 0.15
        )
    ) as factors_json
from base
