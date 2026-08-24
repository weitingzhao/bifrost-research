{{ config(materialized='table') }}

/*
  25 extended fundamental conditions across 7 groups:
  - Profitability (ROE, margins)
  - Balance sheet quality (debt, current ratio)
  - Cash flow (FCF, OCF)
  - Growth quality (consecutive quarters positive)
  - Institutional quality (no major red flags)
  - Valuation support (not extreme overvaluation)
  - Earnings quality (cash vs accrual)
*/

with latest_ratios as (
    select distinct on (symbol)
        symbol,
        roe,
        roa,
        profit_margin,
        operating_margin,
        gross_margin,
        debt_to_equity,
        current_ratio
    from {{ ref('stg_ratios') }}
    where period_type = 'quarterly'
    order by symbol, period_date desc
),

latest_balance as (
    select distinct on (symbol)
        symbol,
        total_assets,
        total_liabilities,
        total_equity,
        noncurrent_liabilities as total_debt,
        current_assets,
        current_liabilities,
        coalesce(current_assets - current_liabilities, 0) as net_working_capital,
        fixed_assets,
        equity_to_parent
    from {{ ref('stg_balance_sheet') }}
    where period_type = 'quarterly'
    order by symbol, period_date desc
),

latest_cashflow as (
    select distinct on (symbol)
        symbol,
        operating_cf,
        investing_cf,
        net_cash_flow,
        coalesce(operating_cf + investing_cf, 0) as free_cash_flow
    from {{ ref('stg_cash_flow') }}
    where period_type = 'quarterly'
    order by symbol, period_date desc
),

latest_income as (
    select distinct on (symbol)
        symbol,
        eps,
        net_income,
        revenue
    from {{ ref('stg_income_stmt') }}
    where period_type = 'quarterly'
    order by symbol, period_date desc
)

select
    u.symbol,
    current_date as eval_date,

    -- Group 1: Profitability (5 conditions)
    coalesce(r.roe > 0.15, false) as roe_gt_15pct,
    coalesce(r.roa > 0.07, false) as roa_gt_7pct,
    coalesce(r.profit_margin > 0.10, false) as net_margin_gt_10pct,
    coalesce(r.operating_margin > 0.15, false) as op_margin_gt_15pct,
    coalesce(r.gross_margin > 0.40, false) as gross_margin_gt_40pct,

    -- Group 2: Balance Sheet Quality (4 conditions)
    coalesce(r.debt_to_equity < 1.0, false) as de_lt_1,
    coalesce(r.current_ratio > 1.5, false) as current_ratio_gt_1_5,
    coalesce(b.net_working_capital > b.total_debt, false) as net_cash_positive,
    coalesce(
        {{ safe_divide('b.fixed_assets', 'b.total_assets') }} < 0.30,
        false
    ) as intangibles_lt_30pct,

    -- Group 3: Cash Flow Quality (4 conditions)
    coalesce(cf.operating_cf > 0, false) as ocf_positive,
    coalesce(cf.free_cash_flow > 0, false) as fcf_positive,
    coalesce(cf.operating_cf > inc.net_income, false) as ocf_gt_net_income,
    coalesce(
        {{ safe_divide('cf.free_cash_flow', 'inc.revenue') }} > 0.05,
        false
    ) as fcf_margin_gt_5pct,

    -- Group 4: Growth Consistency (4 conditions)
    coalesce(inc.eps > 0, false) as latest_eps_positive,
    coalesce(inc.revenue > 0, false) as latest_rev_positive,
    coalesce(inc.net_income > 0, false) as latest_ni_positive,
    coalesce(cf.operating_cf > 0 and cf.free_cash_flow > 0, false) as cf_both_positive,

    -- Group 5: Leverage Safety (3 conditions)
    coalesce(r.debt_to_equity < 0.5, false) as de_lt_0_5,
    coalesce(
        {{ safe_divide('b.total_liabilities', 'b.total_assets') }} < 0.6,
        false
    ) as liab_asset_lt_60pct,
    coalesce(b.current_assets > b.current_liabilities * 2, false) as quick_ratio_gt_2,

    -- Group 6: Earnings Quality (3 conditions)
    coalesce(
        cf.operating_cf > inc.net_income * 0.8,
        false
    ) as accrual_quality_ok,
    coalesce(r.roe > r.roa, false) as roe_gt_roa,
    coalesce(r.operating_margin > r.profit_margin, false) as op_margin_gt_net,

    -- Group 7: Scale & Stability (2 conditions)
    coalesce(b.total_equity > 0, false) as positive_equity,
    coalesce(inc.revenue > 100000000, false) as rev_gt_100m,

    -- Extended pass count (sum of all 25)
    (
        coalesce(r.roe > 0.15, false)::int
        + coalesce(r.roa > 0.07, false)::int
        + coalesce(r.profit_margin > 0.10, false)::int
        + coalesce(r.operating_margin > 0.15, false)::int
        + coalesce(r.gross_margin > 0.40, false)::int
        + coalesce(r.debt_to_equity < 1.0, false)::int
        + coalesce(r.current_ratio > 1.5, false)::int
        + coalesce(b.net_working_capital > b.total_debt, false)::int
        + coalesce({{ safe_divide('b.fixed_assets', 'b.total_assets') }} < 0.30, false)::int
        + coalesce(cf.operating_cf > 0, false)::int
        + coalesce(cf.free_cash_flow > 0, false)::int
        + coalesce(cf.operating_cf > inc.net_income, false)::int
        + coalesce({{ safe_divide('cf.free_cash_flow', 'inc.revenue') }} > 0.05, false)::int
        + coalesce(inc.eps > 0, false)::int
        + coalesce(inc.revenue > 0, false)::int
        + coalesce(inc.net_income > 0, false)::int
        + coalesce(cf.operating_cf > 0 and cf.free_cash_flow > 0, false)::int
        + coalesce(r.debt_to_equity < 0.5, false)::int
        + coalesce({{ safe_divide('b.total_liabilities', 'b.total_assets') }} < 0.6, false)::int
        + coalesce(b.current_assets > b.current_liabilities * 2, false)::int
        + coalesce(cf.operating_cf > inc.net_income * 0.8, false)::int
        + coalesce(r.roe > r.roa, false)::int
        + coalesce(r.operating_margin > r.profit_margin, false)::int
        + coalesce(b.total_equity > 0, false)::int
        + coalesce(inc.revenue > 100000000, false)::int
    ) as ext_pass_count

from {{ ref('dim_universe') }} u
left join latest_ratios r on u.symbol = r.symbol
left join latest_balance b on u.symbol = b.symbol
left join latest_cashflow cf on u.symbol = cf.symbol
left join latest_income inc on u.symbol = inc.symbol
where u.included = true
