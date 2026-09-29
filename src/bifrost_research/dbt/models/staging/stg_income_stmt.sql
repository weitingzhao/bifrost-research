{{ config(materialized='table') }}

-- v1 names first, legacy XBRL names second: see macros/financial_value.sql.
select
    symbol,
    period_date,
    period_type,
    fiscal_year,
    fiscal_quarter,
    fetched_at,
    {{ financial_value('revenue', 'revenues') }} as revenue,
    {{ financial_value('basic_earnings_per_share', 'basic_earnings_per_share') }} as eps,
    {{ financial_value('consolidated_net_income_loss', 'net_income_loss') }} as net_income,
    -- v1 reports gross profit; the legacy shape had no such line, so it was
    -- revenue less costs_and_expenses (all costs, not just cost of revenue).
    coalesce(
        case
            when jsonb_typeof(data -> 'gross_profit') = 'number'
                then (data ->> 'gross_profit')::numeric
        end,
        (data -> 'revenues' ->> 'value')::numeric
        - coalesce((data -> 'costs_and_expenses' ->> 'value')::numeric, 0)
    ) as gross_profit,
    {{ financial_value('operating_income', 'operating_income_loss') }} as operating_income,
    {{ financial_value('cost_of_revenue', 'costs_and_expenses') }} as cost_of_revenue,
    {{ financial_value('total_operating_expenses', 'operating_expenses') }} as operating_expenses
from {{ source('market', 'income_statement') }}
