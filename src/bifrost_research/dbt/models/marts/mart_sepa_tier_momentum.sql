{{ config(materialized='table') }}

/*
  Tier 2: Momentum indicators (10 signals)
  - RSI 14 (approximated via SMA smoothing)
  - MACD (12/26 SMA approximation)
  - ROC 10 / ROC 21
  - RS vs SPY (relative strength line slope)
  - Volume trend (rising vs declining)
  - Price above SMA10
*/

with daily_latest as (
    select distinct on (e.symbol)
        e.symbol,
        e.trade_date,
        e.close,
        e.volume,
        e.volume_ma_50,
        e.sma_10,
        e.roc_10,
        e.roc_21,
        e.return_252d,
        e.price_change
    from {{ ref('int_stock_daily_enriched') }} e
    inner join {{ ref('dim_universe') }} u on e.symbol = u.symbol
    where e.bar_sequence >= 252
    order by e.symbol, e.trade_date desc
),

rsi_calc as (
    select
        e.symbol,
        e.trade_date,
        avg(case when e.price_change > 0 then e.price_change else 0 end)
            over (partition by e.symbol order by e.trade_date rows between 13 preceding and current row) as avg_gain,
        avg(case when e.price_change < 0 then abs(e.price_change) else 0 end)
            over (partition by e.symbol order by e.trade_date rows between 13 preceding and current row) as avg_loss
    from {{ ref('int_stock_daily_enriched') }} e
    inner join {{ ref('dim_universe') }} u on e.symbol = u.symbol
    where e.bar_sequence >= 30
),

latest_rsi as (
    select distinct on (symbol)
        symbol,
        case
            when avg_loss = 0 then 100
            else 100 - (100 / (1 + avg_gain / nullif(avg_loss, 0)))
        end as rsi_14
    from rsi_calc
    order by symbol, trade_date desc
),

macd_calc as (
    select distinct on (e.symbol)
        e.symbol,
        avg(e.close) over (partition by e.symbol order by e.trade_date rows between 11 preceding and current row) as ema_12_approx,
        avg(e.close) over (partition by e.symbol order by e.trade_date rows between 25 preceding and current row) as ema_26_approx
    from {{ ref('int_stock_daily_enriched') }} e
    inner join {{ ref('dim_universe') }} u on e.symbol = u.symbol
    where e.bar_sequence >= 252
    order by e.symbol, e.trade_date desc
),

spy_return as (
    select return_252d as spy_252d_return
    from {{ ref('int_stock_daily_enriched') }}
    where symbol = 'SPY'
    order by trade_date desc
    limit 1
),

vol_trend as (
    select distinct on (e.symbol)
        e.symbol,
        avg(e.volume) over (partition by e.symbol order by e.trade_date rows between 9 preceding and current row) as vol_10,
        e.volume_ma_50
    from {{ ref('int_stock_daily_enriched') }} e
    inner join {{ ref('dim_universe') }} u on e.symbol = u.symbol
    where e.bar_sequence >= 252
    order by e.symbol, e.trade_date desc
)

select
    d.symbol,
    current_date as eval_date,

    -- RSI signals
    coalesce(r.rsi_14 > 50, false) as rsi_above_50,
    coalesce(r.rsi_14 between 40 and 70, false) as rsi_healthy_range,

    -- MACD signal
    coalesce(m.ema_12_approx > m.ema_26_approx, false) as macd_bullish,
    coalesce(
        (m.ema_12_approx - m.ema_26_approx) / nullif(d.close, 0) > 0.01,
        false
    ) as macd_strong,

    -- ROC signals
    coalesce(d.roc_10 > 0, false) as roc_10_positive,
    coalesce(d.roc_21 > 0, false) as roc_21_positive,

    -- RS vs SPY (JOIN instead of correlated subquery)
    coalesce(d.return_252d > (select spy_252d_return from spy_return), false) as rs_gt_spy,

    -- Volume trend signals
    coalesce(v.vol_10 > v.volume_ma_50, false) as volume_expanding,
    coalesce(v.vol_10 > v.volume_ma_50 * 1.5, false) as volume_surge,

    -- Price above SMA10 (from pre-joined daily_latest)
    coalesce(d.close > d.sma_10, false) as price_gt_sma10,

    -- Momentum score (all 10 signals normalized 0-1)
    (
        coalesce(r.rsi_14 > 50, false)::int
        + coalesce(r.rsi_14 between 40 and 70, false)::int
        + coalesce(m.ema_12_approx > m.ema_26_approx, false)::int
        + coalesce((m.ema_12_approx - m.ema_26_approx) / nullif(d.close, 0) > 0.01, false)::int
        + coalesce(d.roc_10 > 0, false)::int
        + coalesce(d.roc_21 > 0, false)::int
        + coalesce(d.return_252d > (select spy_252d_return from spy_return), false)::int
        + coalesce(v.vol_10 > v.volume_ma_50, false)::int
        + coalesce(v.vol_10 > v.volume_ma_50 * 1.5, false)::int
        + coalesce(d.close > d.sma_10, false)::int
    )::numeric / 10.0 as momentum_score,

    -- Raw metrics
    r.rsi_14,
    m.ema_12_approx - m.ema_26_approx as macd_line,
    d.roc_10,
    d.roc_21

from daily_latest d
left join latest_rsi r on d.symbol = r.symbol
left join macd_calc m on d.symbol = m.symbol
left join vol_trend v on d.symbol = v.symbol
