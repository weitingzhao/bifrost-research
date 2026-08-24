{{ config(materialized='table') }}

select
    symbol,
    period_date,
    (data ->> 'shortVolume')::bigint as short_volume,
    (data ->> 'totalVolume')::bigint as total_volume,
    (data ->> 'shortVolumeRatio')::numeric as short_volume_ratio,
    fetched_at
from {{ source('market', 'stock_financials') }}
where report_type = 'short_volume'
