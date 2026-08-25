{{ config(materialized='table') }}

/*
  Options Structure tier — reads features.option_metric_* (Wave 12).
  Score 0–1 aligned with Python sepa_fusion._structure_score (0–100 / 100).
*/

with iv as (
    select distinct on (symbol)
        symbol,
        iv_percentile_1y as iv_percentile
    from {{ source('features', 'option_metric_iv_percentile_daily') }}
    order by symbol, trade_date desc
),

pcr as (
    select distinct on (symbol)
        symbol,
        pcr_oi
    from {{ source('features', 'option_metric_pcr_daily') }}
    order by symbol, trade_date desc
),

gex as (
    select distinct on (symbol)
        symbol,
        spot,
        zero_gamma,
        major_call_wall as call_wall,
        major_put_wall as put_wall
    from {{ source('features', 'option_metric_gex_levels_daily') }}
    order by symbol, trade_date desc, expiry asc
),

joined as (
    select
        u.symbol,
        iv.iv_percentile,
        pcr.pcr_oi,
        gex.spot,
        gex.zero_gamma,
        gex.call_wall,
        gex.put_wall
    from {{ ref('dim_universe') }} u
    left join iv on u.symbol = iv.symbol
    left join pcr on u.symbol = pcr.symbol
    left join gex on u.symbol = gex.symbol
    where u.included = true
),

scored as (
    select
        symbol,
        iv_percentile,
        pcr_oi,
        spot,
        zero_gamma,
        call_wall,
        put_wall,
        case
            when iv_percentile is not null
                then greatest(0, least(100, 100 - iv_percentile))
        end as iv_component,
        case
            when pcr_oi is not null
                then greatest(
                    0,
                    least(100, 100 - least(1.0, abs(pcr_oi - 0.85) / 0.5) * 100)
                )
        end as pcr_component,
        case
            when spot is not null and zero_gamma is not null and zero_gamma > 0
                then greatest(0, least(100, 50 + (spot - zero_gamma) / zero_gamma * 5000))
        end as zg_component,
        case
            when spot is not null
                and call_wall is not null
                and put_wall is not null
                and call_wall > put_wall
                and put_wall > 0
                then greatest(
                    0,
                    least(
                        100,
                        65 - abs((spot - put_wall) / (call_wall - put_wall) - 0.5) * 60
                    )
                )
        end as wall_component
    from joined
)

select
    symbol,
    iv_percentile,
    pcr_oi,
    case
        when (
            (case when iv_component is not null then 1 else 0 end)
            + (case when pcr_component is not null then 1 else 0 end)
            + (case when zg_component is not null then 1 else 0 end)
            + (case when wall_component is not null then 1 else 0 end)
        ) = 0
            then 0.5
        else (
            coalesce(iv_component, 0)
            + coalesce(pcr_component, 0)
            + coalesce(zg_component, 0)
            + coalesce(wall_component, 0)
        ) / (
            (case when iv_component is not null then 1 else 0 end)
            + (case when pcr_component is not null then 1 else 0 end)
            + (case when zg_component is not null then 1 else 0 end)
            + (case when wall_component is not null then 1 else 0 end)
        ) / 100.0
    end as options_structure_score
from scored
where (
    iv_component is not null
    or pcr_component is not null
    or zg_component is not null
    or wall_component is not null
)
