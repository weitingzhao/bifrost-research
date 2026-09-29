{{ config(materialized='table') }}

-- v1 names first, legacy XBRL names second: see macros/financial_value.sql.
select
    symbol,
    period_date,
    period_type,
    fiscal_year,
    fiscal_quarter,
    fetched_at,
    {{ financial_value('total_assets', 'assets') }} as total_assets,
    {{ financial_value('total_liabilities', 'liabilities') }} as total_liabilities,
    {{ financial_value('total_equity', 'equity') }} as total_equity,
    {{ financial_value('total_current_assets', 'current_assets') }} as current_assets,
    {{ financial_value('total_current_liabilities', 'current_liabilities') }}
        as current_liabilities,
    -- v1 has no noncurrent total; it is what the current liabilities leave.
    coalesce(
        case
            when
                jsonb_typeof(data -> 'total_liabilities') = 'number'
                and jsonb_typeof(data -> 'total_current_liabilities') = 'number'
                then
                    (data ->> 'total_liabilities')::numeric
                    - (data ->> 'total_current_liabilities')::numeric
        end,
        (data -> 'noncurrent_liabilities' ->> 'value')::numeric
    ) as noncurrent_liabilities,
    {{ financial_value('property_plant_equipment_net', 'fixed_assets') }} as fixed_assets,
    {{ financial_value('total_equity_attributable_to_parent', 'equity_attributable_to_parent') }}
        as equity_to_parent
from {{ source('market', 'balance_sheet') }}
