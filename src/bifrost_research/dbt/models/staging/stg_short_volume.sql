{{ config(materialized='view') }}

-- A view since 0.120.0. As a table it rewrote all 7.2M rows (650 MB) every run for
-- its one reader, mart_sepa_tier_sentiment, which keeps each symbol's last 20
-- sessions. Owned by analytics_writer like the rest of dw_stock, and nothing else
-- depends on the relation, so dbt's drop-and-recreate is safe (2026-09-25).

/*
  Daily short volume, full market by date (raw_market.short_volume).

  The vendor's keys are snake_case, not camelCase — the previous model read
  'shortVolume' and friends and therefore produced nothing but nulls. Its
  short_volume_ratio is a percent (58.96), while this model's contract calls
  short_volume_ratio a ratio, so it is computed from the two volumes and falls
  back to the vendor's percent scaled to a fraction.
*/

select
    symbol,
    period_date,
    (data ->> 'short_volume')::numeric      as short_volume,
    (data ->> 'total_volume')::numeric      as total_volume,
    (data ->> 'exempt_volume')::numeric     as exempt_volume,
    (data ->> 'non_exempt_volume')::numeric as non_exempt_volume,
    coalesce(
        nullif((data ->> 'short_volume')::numeric, 0)
            / nullif((data ->> 'total_volume')::numeric, 0),
        (data ->> 'short_volume_ratio')::numeric / 100.0
    ) as short_volume_ratio,
    (data ->> 'short_volume_ratio')::numeric as short_volume_pct,
    fetched_at
from {{ source('market', 'short_volume') }}
