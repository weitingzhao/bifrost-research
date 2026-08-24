{{ config(materialized='table') }}

select
    f.symbol,
    current_date as eval_date,
    f.pass_count as fund_pass_count,
    f.insufficient_data as fund_insufficient,
    t.pass_count as tech_pass_count,
    f.pass_count + t.pass_count as combined_pass_count,

    -- Weighted composite score (fundamental 40% + technical 40% + momentum 10% + structure 10%)
    (
        f.pass_count * 1.0 / 8 * 0.4
        + t.pass_count * 1.0 / 11 * 0.4
        + coalesce(m.momentum_score, 0) * 0.1
        + coalesce(s.structure_score, 0) * 0.1
    ) as composite_score,

    m.momentum_score,
    s.structure_score,
    se.sentiment_score

from {{ ref('mart_sepa_fundamental_eval') }} f
inner join {{ ref('mart_sepa_technical_eval') }} t using (symbol)
left join {{ ref('mart_sepa_tier_momentum') }} m using (symbol)
left join {{ ref('mart_sepa_tier_structure') }} s using (symbol)
left join {{ ref('mart_sepa_tier_sentiment') }} se using (symbol)
