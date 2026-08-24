{{ config(materialized='table') }}

select
    symbol,
    period_date,
    (data ->> 'shortInterest')::numeric as shares_short,
    (data ->> 'daysToCover')::numeric as days_to_cover,
    (data ->> 'shortPercentOfFloat')::numeric as short_pct_float,
    fetched_at
from {{ source('market', 'short_interest') }}
