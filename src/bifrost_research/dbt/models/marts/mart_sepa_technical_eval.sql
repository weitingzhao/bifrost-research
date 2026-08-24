{{ config(materialized='table') }}

with latest as (
    select distinct on (e.symbol)
        e.symbol,
        e.trade_date,
        e.close,
        e.volume,
        e.sma_50,
        e.sma_150,
        e.sma_200,
        e.volume_ma_50,
        e.low_52w,
        e.high_52w,
        e.sma_200_20d_ago
    from {{ ref('int_stock_daily_enriched') }} e
    inner join {{ ref('dim_universe') }} u on e.symbol = u.symbol
    where e.bar_sequence >= 252
    order by e.symbol, e.trade_date desc
),

latest_crs as (
    select distinct on (symbol)
        symbol,
        crs_percentile,
        return_252d
    from {{ ref('int_stock_crs') }}
    order by symbol, trade_date desc
)

select
    l.symbol,
    current_date as eval_date,
    l.trade_date as price_date,

    -- 11 core conditions
    coalesce(l.volume_ma_50 > 100000, false) as avg_volume_50_gt_threshold,
    coalesce(l.close >= l.low_52w * 1.3, false) as close_ge_low52_x_1_3,
    coalesce(l.close >= l.high_52w * 0.75, false) as close_ge_high52_x_0_75,
    coalesce(l.sma_50 > l.sma_150, false) as sma50_gt_sma150,
    coalesce(l.sma_50 > l.sma_200, false) as sma50_gt_sma200,
    coalesce(l.sma_150 > l.sma_200, false) as sma150_gt_sma200,
    coalesce(l.sma_200 > l.sma_200_20d_ago, false) as sma200_rising_1m,
    coalesce(l.close > l.sma_50, false) as price_gt_sma50,
    coalesce(l.close > l.sma_150, false) as price_gt_sma150,
    coalesce(l.close > l.sma_200, false) as price_gt_sma200,
    coalesce(c.crs_percentile >= 70, false) as crs_ge_70,

    -- Pass count summary
    (
        coalesce(l.volume_ma_50 > 100000, false)::int
        + coalesce(l.close >= l.low_52w * 1.3, false)::int
        + coalesce(l.close >= l.high_52w * 0.75, false)::int
        + coalesce(l.sma_50 > l.sma_150, false)::int
        + coalesce(l.sma_50 > l.sma_200, false)::int
        + coalesce(l.sma_150 > l.sma_200, false)::int
        + coalesce(l.sma_200 > l.sma_200_20d_ago, false)::int
        + coalesce(l.close > l.sma_50, false)::int
        + coalesce(l.close > l.sma_150, false)::int
        + coalesce(l.close > l.sma_200, false)::int
        + coalesce(c.crs_percentile >= 70, false)::int
    ) as pass_count,

    -- Raw metrics for inspector
    l.close,
    l.sma_50,
    l.sma_150,
    l.sma_200,
    l.volume_ma_50,
    l.low_52w,
    l.high_52w,
    c.crs_percentile,
    c.return_252d

from latest l
left join latest_crs c on l.symbol = c.symbol
