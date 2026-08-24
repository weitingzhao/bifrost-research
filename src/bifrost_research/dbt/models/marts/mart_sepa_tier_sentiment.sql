{{ config(materialized='table') }}

/*
  Tier 4: Sentiment signals from short interest and short volume
  - Short interest change (declining = bullish)
  - Days to cover threshold
  - Short volume ratio trend
*/

with latest_si as (
    select distinct on (symbol)
        symbol,
        period_date as si_date,
        shares_short,
        days_to_cover,
        short_pct_float
    from {{ ref('stg_short_interest') }}
    order by symbol, period_date desc
),

prev_si as (
    select distinct on (symbol)
        symbol,
        shares_short as prev_shares_short,
        short_pct_float as prev_short_pct_float
    from (
        select
            symbol,
            shares_short,
            short_pct_float,
            row_number() over (partition by symbol order by period_date desc) as rn
        from {{ ref('stg_short_interest') }}
    ) ranked
    where rn = 2
    order by symbol
),

-- Short volume: latest 5-day average vs 20-day average
sv_recent as (
    select
        symbol,
        avg(short_volume_ratio) filter (where rn <= 5) as sv_ratio_5d,
        avg(short_volume_ratio) filter (where rn <= 20) as sv_ratio_20d,
        max(short_volume_ratio) filter (where rn = 1) as sv_ratio_latest
    from (
        select
            symbol,
            period_date,
            short_volume_ratio,
            row_number() over (partition by symbol order by period_date desc) as rn
        from {{ ref('stg_short_volume') }}
    ) ranked
    where rn <= 20
    group by symbol
)

select
    u.symbol,
    current_date as eval_date,

    -- Short interest signals (declining SI = bullish squeeze potential)
    coalesce(
        si.shares_short < psi.prev_shares_short,
        false
    ) as si_declining,
    coalesce(si.short_pct_float < 0.10, false) as low_short_float,
    coalesce(si.days_to_cover > 5, false) as high_days_to_cover,
    coalesce(
        si.short_pct_float < psi.prev_short_pct_float,
        false
    ) as short_float_declining,

    -- Short volume signals
    coalesce(sv.sv_ratio_latest < 0.30, false) as low_short_volume,
    coalesce(sv.sv_ratio_5d < sv.sv_ratio_20d, false) as sv_ratio_declining,

    -- Sentiment score (normalized 0-1)
    (
        coalesce(si.shares_short < psi.prev_shares_short, false)::int
        + coalesce(si.short_pct_float < 0.10, false)::int
        + coalesce(si.days_to_cover > 5, false)::int
        + coalesce(si.short_pct_float < psi.prev_short_pct_float, false)::int
        + coalesce(sv.sv_ratio_latest < 0.30, false)::int
        + coalesce(sv.sv_ratio_5d < sv.sv_ratio_20d, false)::int
    )::numeric / 6.0 as sentiment_score,

    -- Raw metrics
    si.shares_short,
    si.days_to_cover,
    si.short_pct_float,
    psi.prev_shares_short,
    sv.sv_ratio_latest,
    sv.sv_ratio_5d,
    sv.sv_ratio_20d

from {{ ref('dim_universe') }} u
left join latest_si si on u.symbol = si.symbol
left join prev_si psi on u.symbol = psi.symbol
left join sv_recent sv on u.symbol = sv.symbol
where u.included = true
