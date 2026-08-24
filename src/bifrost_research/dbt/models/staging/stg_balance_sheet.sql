{{ config(materialized='table') }}

select
    symbol,
    period_date,
    period_type,
    fiscal_year,
    fiscal_quarter,
    (data -> 'assets' ->> 'value')::numeric as total_assets,
    (data -> 'liabilities' ->> 'value')::numeric as total_liabilities,
    (data -> 'equity' ->> 'value')::numeric as total_equity,
    (data -> 'current_assets' ->> 'value')::numeric as current_assets,
    (data -> 'current_liabilities' ->> 'value')::numeric as current_liabilities,
    (data -> 'noncurrent_liabilities' ->> 'value')::numeric as noncurrent_liabilities,
    (data -> 'fixed_assets' ->> 'value')::numeric as fixed_assets,
    (data -> 'equity_attributable_to_parent' ->> 'value')::numeric as equity_to_parent,
    fetched_at
from {{ source('market', 'balance_sheet') }}
