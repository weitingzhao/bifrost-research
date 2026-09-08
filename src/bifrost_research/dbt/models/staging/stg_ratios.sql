{{ config(materialized='table') }}

/*
  Financial ratios, full market by date, from Massive's Financials & Ratios
  plan (raw_market.ratios). This model used to be a hard-coded empty table:
  the note said the vendor had no 'ratios' report, which stopped being true
  when the plugin started pulling /stocks/vX/ratios daily for the whole market.

  Keys are the vendor's own snake_case. profit_margin / operating_margin /
  gross_margin are not in the ratios payload — downstream models still derive
  them from the income statement — so they stay null here rather than being
  invented.
*/

select
    symbol,
    period_date,
    period_type,
    fiscal_year,
    fiscal_quarter,

    -- Returns and leverage (the columns downstream marts already read)
    (data ->> 'return_on_equity')::numeric        as roe,
    (data ->> 'return_on_assets')::numeric        as roa,
    null::numeric                                  as profit_margin,
    null::numeric                                  as operating_margin,
    null::numeric                                  as gross_margin,
    (data ->> 'debt_to_equity')::numeric          as debt_to_equity,
    (data ->> 'current')::numeric                 as current_ratio,
    (data ->> 'quick')::numeric                   as quick_ratio,
    (data ->> 'cash')::numeric                    as cash_ratio,

    -- Valuation
    (data ->> 'price_to_earnings')::numeric       as price_to_earnings,
    (data ->> 'price_to_book')::numeric           as price_to_book,
    (data ->> 'price_to_sales')::numeric          as price_to_sales,
    (data ->> 'price_to_cash_flow')::numeric      as price_to_cash_flow,
    (data ->> 'price_to_free_cash_flow')::numeric as price_to_free_cash_flow,
    (data ->> 'ev_to_ebitda')::numeric            as ev_to_ebitda,
    (data ->> 'ev_to_sales')::numeric             as ev_to_sales,
    (data ->> 'dividend_yield')::numeric          as dividend_yield,
    (data ->> 'earnings_per_share')::numeric      as earnings_per_share,

    -- Size and liquidity
    (data ->> 'market_cap')::numeric              as market_cap,
    (data ->> 'enterprise_value')::numeric        as enterprise_value,
    (data ->> 'free_cash_flow')::numeric          as free_cash_flow,
    (data ->> 'price')::numeric                   as price,
    (data ->> 'average_volume')::numeric          as average_volume,

    fetched_at
from {{ source('market', 'ratios') }}
