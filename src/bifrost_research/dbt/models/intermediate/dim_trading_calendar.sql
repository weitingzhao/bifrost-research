{{ config(materialized='table') }}

-- Every date from the first year the holiday feed covers, with whether NYSE
-- traded. Two faults fixed 2026-09-26:
--   * early-close days (the day after Thanksgiving, Christmas Eve) trade; they
--     were counted with the closures, and 2026-11-27 and 12-24 would have read
--     as shut;
--   * the series began in 2015 while us_market_holiday begins in 2020, so every
--     holiday of 2015-2019 read as a session. The calendar starts where its
--     source does.

with all_dates as (
    select d::date as calendar_date
    from generate_series('2020-01-01'::date, current_date, '1 day'::interval) as d
),

holidays as (
    -- NYSE + NASDAQ both publish the same closed days; without DISTINCT the
    -- LEFT JOIN duplicated trade_date and broke unique_*.
    select distinct holiday_date
    from {{ source('market', 'us_market_holiday') }}
    where status = 'closed'
)

select
    a.calendar_date as trade_date,
    (extract(dow from a.calendar_date) not in (0, 6)
     and h.holiday_date is null) as is_trading_day
from all_dates a
left join holidays h on a.calendar_date = h.holiday_date
