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
    {{ financial_value('change_in_cash_and_equivalents', 'net_cash_flow') }} as net_cash_flow,
    -- Capex and free cash flow are v1 lines only. The legacy shape has no capex
    -- line, and investing cash flow is not a stand-in for one (it also holds
    -- securities and acquisitions), so both stay NULL on a legacy row.
    -- The vendor signs capex as a cash flow, like investing_cf: negative when
    -- cash is spent (3,239 of 3,372 rows that carry it, 28 names, 2026-09-29),
    -- 0 where a bank reports no line, and positive in the few periods where it
    -- nets to an inflow (there, too, it adds up with the other investing lines
    -- to investing_cf). Free cash flow therefore adds it.
    {{ financial_v1_value('purchase_of_property_plant_and_equipment') }} as capex,
    {{ financial_v1_value('net_cash_from_operating_activities') }}
    + {{ financial_v1_value('purchase_of_property_plant_and_equipment') }}
        as free_cash_flow
from {{ source('market', 'cash_flow') }}
