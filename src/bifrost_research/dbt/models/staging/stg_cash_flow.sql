{{ config(materialized='table') }}

select
    symbol,
    period_date,
    period_type,
    fiscal_year,
    fiscal_quarter,
    (data -> 'net_cash_flow_from_operating_activities' ->> 'value')::numeric as operating_cf,
    (data -> 'net_cash_flow_from_investing_activities' ->> 'value')::numeric as investing_cf,
    (data -> 'net_cash_flow_from_financing_activities' ->> 'value')::numeric as financing_cf,
    (data -> 'net_cash_flow' ->> 'value')::numeric as net_cash_flow,
    fetched_at
from {{ source('market', 'cash_flow') }}
