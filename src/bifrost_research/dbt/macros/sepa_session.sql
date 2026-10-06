{#
  The session a SEPA evaluation describes: the newest bar in the source it reads.

  Until 2026-10-06 (TD-87) the seven SEPA marts stamped ``current_date``. The
  database clock is UTC and research_trading_day fires at 22:30 New York, i.e.
  02:30 UTC the next day, so Monday's session was stored as Tuesday and Friday's
  as Saturday; signal_hit, the backtests and every join on trade_date then paired
  SEPA with the following session. The data knows which session it is; the clock
  does not. Postgres runs the uncorrelated subquery once per statement.
  Ratchet: the sepa_session_is_newest_trading_day test on mart_sepa_feature_daily.
#}
{% macro sepa_session() %}
    (select max(sepa_bars.trade_date) from {{ ref('int_stock_daily_enriched') }} as sepa_bars)
{% endmacro %}
