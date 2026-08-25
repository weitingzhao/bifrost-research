{{ config(materialized='table') }}

select
    f.symbol,
    current_date as eval_date,
    f.pass_count as fund_pass_count,
    f.insufficient_data as fund_insufficient,
    t.pass_count as tech_pass_count,
    f.pass_count + t.pass_count as combined_pass_count,

    -- Weighted composite (30F + 35T + 20M + 15O) — Wave 12 canonical weights
    (
        f.pass_count * 1.0 / 8 * 0.30
        + t.pass_count * 1.0 / 11 * 0.35
        + coalesce(m.momentum_score, 0) * 0.20
        + coalesce(o.options_structure_score, 0.5) * 0.15
    ) as composite_score,

    m.momentum_score,
    o.options_structure_score as structure_score,
    se.sentiment_score

from {{ ref('mart_sepa_fundamental_eval') }} f
inner join {{ ref('mart_sepa_technical_eval') }} t using (symbol)
left join {{ ref('mart_sepa_tier_momentum') }} m using (symbol)
left join {{ ref('mart_sepa_tier_options') }} o using (symbol)
left join {{ ref('mart_sepa_tier_sentiment') }} se using (symbol)
