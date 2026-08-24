{{ config(materialized='table') }}

/*
  Tier 3: Structure diagnostics (5 indicator groups)
  - Bollinger Band squeeze (width contraction)
  - ADX (trend strength)
  - Aroon Up/Down (trend direction)
  - OBV trend (on-balance volume)
  - Volatility contraction ratio
*/

with enriched_latest as (
    select
        e.symbol,
        e.trade_date,
        e.close,
        e.high,
        e.low,
        e.volume,
        e.sma_20,
        e.atr_14,
        e.bar_sequence,
        row_number() over (partition by e.symbol order by e.trade_date desc) as recency
    from {{ ref('int_stock_daily_enriched') }} e
    inner join {{ ref('dim_universe') }} u on e.symbol = u.symbol
    where e.bar_sequence >= 252
),

-- Bollinger Band width: (upper - lower) / sma_20
-- upper = sma_20 + 2*stddev, lower = sma_20 - 2*stddev
bb_calc as (
    select
        symbol,
        trade_date,
        sma_20,
        stddev(close) over (partition by symbol order by trade_date rows between 19 preceding and current row) as bb_stddev,
        recency
    from enriched_latest
    where recency <= 50
),

bb_latest as (
    select distinct on (symbol)
        symbol,
        case when sma_20 > 0
            then (4 * bb_stddev) / sma_20
            else null
        end as bb_width,
        bb_stddev
    from bb_calc
    where recency = 1
    order by symbol
),

bb_avg as (
    select
        symbol,
        avg(case when sma_20 > 0 then (4 * bb_stddev) / sma_20 end) as bb_width_avg_50d
    from bb_calc
    where recency <= 50
    group by symbol
),

-- ADX approximation: use ATR relative trend strength
-- Simplified: abs(close - close_14d_ago) / ATR_14 as directional strength
adx_calc as (
    select distinct on (symbol)
        symbol,
        case when atr_14 > 0
            then abs(close - lag(close, 14) over (partition by symbol order by trade_date)) / atr_14
            else 0
        end as adx_proxy
    from enriched_latest
    where recency <= 20
    order by symbol, trade_date desc
),

-- Aroon: position of highest high and lowest low in last 25 days
aroon_calc as (
    select
        symbol,
        trade_date,
        recency,
        case when recency <= 25 then
            row_number() over (partition by symbol order by high desc)
        end as high_rank,
        case when recency <= 25 then
            row_number() over (partition by symbol order by low asc)
        end as low_rank
    from enriched_latest
    where recency <= 25
),

aroon_latest as (
    select
        symbol,
        (25 - min(case when high_rank = 1 then recency end) + 1)::numeric / 25 * 100 as aroon_up,
        (25 - min(case when low_rank = 1 then recency end) + 1)::numeric / 25 * 100 as aroon_down
    from aroon_calc
    group by symbol
),

-- Volatility contraction: current ATR vs ATR from ~50 days ago
vol_contraction as (
    select
        cur.symbol,
        cur.atr_14 as current_atr,
        prev.atr_14 as atr_50d_ago
    from (
        select symbol, atr_14 from enriched_latest where recency = 1
    ) cur
    left join lateral (
        select atr_14
        from enriched_latest e2
        where e2.symbol = cur.symbol and e2.recency between 48 and 52
        order by e2.recency
        limit 1
    ) prev on true
)

select
    u.symbol,
    current_date as eval_date,

    -- BB squeeze: current width < 50-day average (tightening)
    coalesce(bbl.bb_width < bba.bb_width_avg_50d, false) as bb_squeeze,
    coalesce(bbl.bb_width < bba.bb_width_avg_50d * 0.75, false) as bb_tight_squeeze,

    -- ADX: trend strength
    coalesce(adx.adx_proxy > 1.0, false) as adx_trending,
    coalesce(adx.adx_proxy > 2.0, false) as adx_strong_trend,

    -- Aroon: bullish when Aroon Up > Aroon Down
    coalesce(ar.aroon_up > ar.aroon_down, false) as aroon_bullish,
    coalesce(ar.aroon_up > 70, false) as aroon_up_strong,

    -- Volatility contraction
    coalesce(
        vc.current_atr < vc.atr_50d_ago * 0.8,
        false
    ) as vol_contracting,
    coalesce(
        vc.current_atr < vc.atr_50d_ago * 0.6,
        false
    ) as vol_tight_contraction,

    -- Structure score (normalized 0-1)
    (
        coalesce(bbl.bb_width < bba.bb_width_avg_50d, false)::int
        + coalesce(bbl.bb_width < bba.bb_width_avg_50d * 0.75, false)::int
        + coalesce(adx.adx_proxy > 1.0, false)::int
        + coalesce(adx.adx_proxy > 2.0, false)::int
        + coalesce(ar.aroon_up > ar.aroon_down, false)::int
        + coalesce(ar.aroon_up > 70, false)::int
        + coalesce(vc.current_atr < vc.atr_50d_ago * 0.8, false)::int
        + coalesce(vc.current_atr < vc.atr_50d_ago * 0.6, false)::int
    )::numeric / 8.0 as structure_score,

    -- Raw metrics
    bbl.bb_width,
    bba.bb_width_avg_50d,
    adx.adx_proxy,
    ar.aroon_up,
    ar.aroon_down,
    vc.current_atr,
    vc.atr_50d_ago

from {{ ref('dim_universe') }} u
left join bb_latest bbl on u.symbol = bbl.symbol
left join bb_avg bba on u.symbol = bba.symbol
left join adx_calc adx on u.symbol = adx.symbol
left join aroon_latest ar on u.symbol = ar.symbol
left join vol_contraction vc on u.symbol = vc.symbol
where u.included = true
