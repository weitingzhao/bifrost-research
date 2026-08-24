{{ config(materialized='table') }}

select
    symbol,
    name,
    primary_exchange,
    currency,
    cik,
    composite_figi,
    true as included
from {{ source('market', 'ticker') }}
where instrument_type = 'CS'
  and market = 'stocks'
  and active = true
  and currency = 'usd'
