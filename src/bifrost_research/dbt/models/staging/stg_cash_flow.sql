{{ config(materialized='table') }}

-- v1 names first, legacy XBRL names second: see macros/financial_value.sql.
select
    symbol,
    period_date,
    period_type,
    fiscal_year,
    fiscal_quarter,
    fetched_at,
    {{ financial_value(
        'net_cash_from_operating_activities', 'net_cash_flow_from_operating_activities'
    ) }}
        as operating_cf,
    {{ financial_value(
        'net_cash_from_investing_activities', 'net_cash_flow_from_investing_activities'
    ) }}
        as investing_cf,
    {{ financial_value(
        'net_cash_from_financing_activities', 'net_cash_flow_from_financing_activities'
    ) }}
        as financing_cf,
    {{ financial_value('change_in_cash_and_equivalents', 'net_cash_flow') }} as net_cash_flow
from {{ source('market', 'cash_flow') }}
