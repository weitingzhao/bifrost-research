{{ config(materialized='table') }}

with all_dates as (
    select d::date as calendar_date
    from generate_series('2015-01-01'::date, current_date, '1 day'::interval) as d
),

holidays as (
    -- NYSE + NASDAQ both publish the same closed/early-close days; without
    -- DISTINCT the LEFT JOIN duplicated trade_date and broke unique_*.
    select distinct holiday_date
    from {{ source('market', 'us_market_holiday') }}
    where status in ('closed', 'early-close')
)

select
    a.calendar_date as trade_date,
    (extract(dow from a.calendar_date) not in (0, 6)
     and h.holiday_date is null) as is_trading_day
from all_dates a
left join holidays h on a.calendar_date = h.holiday_date
