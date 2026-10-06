/*
  TD-87 ratchet: a SEPA mart's session is the newest bar in its source, and a
  trading day. Returns the offending dates (none when the mart is right).

  A ``current_date`` stamp fails the first half every night it runs after UTC
  midnight (22:30 New York is 02:30 UTC), and a Friday session written as
  Saturday fails the second half. dim_trading_calendar marks weekends and the
  holidays raw_market.us_market_holiday lists as closed.
*/

{% test sepa_session_is_newest_trading_day(model, column_name) %}

with stamped as (
    select distinct {{ column_name }} as session_date
    from {{ model }}
),

newest as (
    select max(trade_date) as bar_date
    from {{ ref('int_stock_daily_enriched') }}
)

select
    s.session_date,
    n.bar_date as newest_bar_date,
    c.is_trading_day
from stamped as s
cross join newest as n
left join {{ ref('dim_trading_calendar') }} as c
    on s.session_date = c.trade_date
where
    s.session_date is distinct from n.bar_date
    or c.is_trading_day is not true

{% endtest %}
