{{ config(materialized='table') }}

/*
  FINRA short interest by settlement date (raw_market.short_interest).

  Keys are the vendor's snake_case; the previous model read camelCase and
  produced nulls. short_pct_float has no source: shares outstanding / float is
  not part of the current subscriptions (the float endpoint answers 404), so
  it is null here rather than guessed, and the signals that need it stay off
  until the plan covers float.
*/

select
    symbol,
    period_date,
    (data ->> 'short_interest')::numeric   as shares_short,
    (data ->> 'days_to_cover')::numeric    as days_to_cover,
    (data ->> 'avg_daily_volume')::numeric as avg_daily_volume,
    null::numeric                           as short_pct_float,
    fetched_at
from {{ source('market', 'short_interest') }}
