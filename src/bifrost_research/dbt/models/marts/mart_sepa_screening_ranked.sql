{{ config(materialized='table') }}

select
    symbol,
    fund_pass_count,
    fund_insufficient,
    tech_pass_count,
    momentum_score,
    structure_score,
    sentiment_score,
    eval_date,
    combined_pass_count,
    composite_score,
    rank() over (order by composite_score desc) as overall_rank,
    percent_rank() over (order by composite_score) as percentile,
    ntile(10) over (order by composite_score desc) as decile
from {{ ref('mart_sepa_composite_score') }}
where not fund_insufficient
