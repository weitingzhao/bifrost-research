{{
  config(
    materialized='incremental',
    unique_key=['symbol', 'trade_date'],
    pre_hook=["SET LOCAL statement_timeout = '0'"]
  )
}}

/*
  Incremental loads only pull a lookback slice of market.stock_daily.
  Computing row_number() on that slice alone resets bar_sequence to 1..N
  and overwrites history — breaking SEPA filters that require bar_sequence >= 252.

  Fix: for incremental runs, union retained history (outside the lookback)
  with the fresh slice before window functions, so sequences stay contiguous.
*/

{% set lookback_days = 210 %}

with fresh as (
    select
        d.symbol,
        d.bar_date as trade_date,
        d.open,
        d.high,
        d.low,
        d.close,
        d.volume
    from {{ source('market', 'stock_daily') }} d
    inner join {{ ref('dim_universe') }} u on d.symbol = u.symbol
    {% if is_incremental() %}
    where d.bar_date > (
        select max(trade_date) - interval '{{ lookback_days }} days' from {{ this }}
    )
    {% endif %}
),

{% if is_incremental() %}
retained as (
    select
        symbol,
        trade_date,
        open,
        high,
        low,
        close,
        volume
    from {{ this }}
    where trade_date <= (
        select max(trade_date) - interval '{{ lookback_days }} days' from {{ this }}
    )
),

source_data as (
    select * from retained
    union all
    select * from fresh
),
{% else %}
source_data as (
    select * from fresh
),
{% endif %}

-- Layer 1: single-pass window functions (lag, row_number, simple aggregates)
with_prev as (
    select
        d.symbol,
        d.trade_date,
        d.open,
        d.high,
        d.low,
        d.close,
        d.volume,

        lag(d.close) over w as prev_close,
        lag(d.close, 10) over w as close_10d_ago,
        lag(d.close, 21) over w as close_21d_ago,
        lag(d.close, 252) over w as close_252d_ago,

        row_number() over (partition by d.symbol order by d.trade_date) as bar_sequence,

        avg(d.close) over (partition by d.symbol order by d.trade_date
            rows between 9 preceding and current row) as sma_10,
        avg(d.close) over (partition by d.symbol order by d.trade_date
            rows between 19 preceding and current row) as sma_20,
        avg(d.close) over (partition by d.symbol order by d.trade_date
            rows between 49 preceding and current row) as sma_50,
        avg(d.close) over (partition by d.symbol order by d.trade_date
            rows between 149 preceding and current row) as sma_150,
        avg(d.close) over (partition by d.symbol order by d.trade_date
            rows between 199 preceding and current row) as sma_200,

        avg(d.volume) over (partition by d.symbol order by d.trade_date
            rows between 49 preceding and current row) as volume_ma_50,

        min(d.low) over (partition by d.symbol order by d.trade_date
            rows between 251 preceding and current row) as low_52w,
        max(d.high) over (partition by d.symbol order by d.trade_date
            rows between 251 preceding and current row) as high_52w

    from source_data d
    window w as (partition by d.symbol order by d.trade_date)
),

with_true_range as (
    select
        *,
        greatest(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        ) as true_range
    from with_prev
),

with_atr as (
    select
        *,
        avg(true_range) over (
            partition by symbol order by trade_date
            rows between 13 preceding and current row
        ) as atr_14,

        lag(sma_200, 20) over (
            partition by symbol order by trade_date
        ) as sma_200_20d_ago
    from with_true_range
)

select
    symbol,
    trade_date,
    open,
    high,
    low,
    close,
    volume,
    sma_10,
    sma_20,
    sma_50,
    sma_150,
    sma_200,
    volume_ma_50,
    low_52w,
    high_52w,
    atr_14,
    sma_200_20d_ago,
    close - prev_close as price_change,
    (close / nullif(close_10d_ago, 0) - 1) as roc_10,
    (close / nullif(close_21d_ago, 0) - 1) as roc_21,
    (close / nullif(close_252d_ago, 0) - 1) as return_252d,
    bar_sequence
from with_atr
{% if is_incremental() %}
where trade_date > (
    select max(trade_date) - interval '{{ lookback_days }} days' from {{ this }}
)
{% endif %}
