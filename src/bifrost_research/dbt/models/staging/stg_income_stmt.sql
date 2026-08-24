{{ config(materialized='table') }}

select
    symbol,
    period_date,
    period_type,
    fiscal_year,
    fiscal_quarter,
    (data -> 'revenues' ->> 'value')::numeric as revenue,
    (data -> 'basic_earnings_per_share' ->> 'value')::numeric as eps,
    (data -> 'net_income_loss' ->> 'value')::numeric as net_income,
    (data -> 'revenues' ->> 'value')::numeric - coalesce((data -> 'costs_and_expenses' ->> 'value')::numeric, 0) as gross_profit,
    (data -> 'operating_income_loss' ->> 'value')::numeric as operating_income,
    (data -> 'costs_and_expenses' ->> 'value')::numeric as cost_of_revenue,
    (data -> 'operating_expenses' ->> 'value')::numeric as operating_expenses,
    fetched_at
from {{ source('market', 'stock_financials') }}
where report_type = 'income_statement'
