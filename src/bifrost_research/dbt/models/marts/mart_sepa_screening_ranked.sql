{{ config(materialized='table') }}

select
    *,
    rank() over (order by composite_score desc) as overall_rank,
    percent_rank() over (order by composite_score) as percentile,
    ntile(10) over (order by composite_score desc) as decile
from {{ ref('mart_sepa_composite_score') }}
where not fund_insufficient
