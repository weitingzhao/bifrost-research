{{ config(materialized='table') }}

{# Polygon API does not provide a 'ratios' report_type.
   Ratios are computed from income_statement + balance_sheet in downstream models.
   This model is a placeholder that always returns an empty table. #}

select
    null::text as symbol,
    null::date as period_date,
    null::text as period_type,
    null::integer as fiscal_year,
    null::integer as fiscal_quarter,
    null::numeric as roe,
    null::numeric as roa,
    null::numeric as profit_margin,
    null::numeric as operating_margin,
    null::numeric as gross_margin,
    null::numeric as debt_to_equity,
    null::numeric as current_ratio,
    null::timestamptz as fetched_at
where false
