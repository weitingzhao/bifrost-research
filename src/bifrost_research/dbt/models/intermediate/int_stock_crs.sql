{{ config(materialized='table') }}

select
    e.symbol,
    e.trade_date,
    e.return_252d,
    percent_rank() over (
        partition by e.trade_date
        order by e.return_252d
    ) * 100 as crs_percentile
from {{ ref('int_stock_daily_enriched') }} e
inner join {{ ref('dim_universe') }} u on e.symbol = u.symbol
where e.return_252d is not null
  and e.bar_sequence >= 252
