{#-
  One number from a raw_market financials row (income_statement, balance_sheet,
  cash_flow), whichever shape the row is in.

  The plugin writes the vendor's v1 statements (flat, standardized names:
  revenue, total_assets, net_cash_from_operating_activities); the legacy
  /vX/reference/financials endpoint it used before is retired on 2026-10-09.
  Symbols the v1 source does not cover keep their legacy rows, in the XBRL
  shape with one object per concept: revenues: {value, unit, label, order}.

  Some names exist in both shapes (basic_earnings_per_share, gross_profit,
  cost_of_revenue): an object there is the legacy concept, not a v1 number, so
  the v1 branch reads the key only when its value is a number.
-#}
{% macro financial_value(v1_key, legacy_key) -%}
    coalesce(
        case
            when jsonb_typeof(data -> '{{ v1_key }}') = 'number'
                then (data ->> '{{ v1_key }}')::numeric
        end,
        (data -> '{{ legacy_key }}' ->> 'value')::numeric
    )
{%- endmacro %}
