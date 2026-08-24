{{ config(materialized='table') }}

with quarterly_ranked as (
    select
        symbol,
        fiscal_year,
        fiscal_quarter,
        period_date,
        eps,
        revenue,
        net_income,
        gross_profit,
        row_number() over (partition by symbol order by period_date desc) as q_rank,
        lag(eps, 4) over (partition by symbol order by period_date) as eps_yoy_base,
        lag(revenue, 4) over (partition by symbol order by period_date) as rev_yoy_base,
        lag(eps, 8) over (partition by symbol order by period_date) as eps_yoy_base_2,
        lag(revenue, 8) over (partition by symbol order by period_date) as rev_yoy_base_2,
        lag(eps, 12) over (partition by symbol order by period_date) as eps_yoy_base_3,
        lag(revenue, 12) over (partition by symbol order by period_date) as rev_yoy_base_3
    from {{ ref('stg_income_stmt') }}
    where period_type = 'quarterly'
),

annual_ranked as (
    select
        symbol,
        fiscal_year,
        period_date,
        eps,
        revenue,
        row_number() over (partition by symbol order by fiscal_year desc) as fy_rank,
        lag(eps) over (partition by symbol order by fiscal_year) as eps_fy_prev,
        lag(revenue) over (partition by symbol order by fiscal_year) as rev_fy_prev,
        lag(eps, 2) over (partition by symbol order by fiscal_year) as eps_fy_prev2,
        lag(eps, 3) over (partition by symbol order by fiscal_year) as eps_fy_3y_ago,
        lag(revenue, 3) over (partition by symbol order by fiscal_year) as rev_fy_3y_ago
    from {{ ref('stg_income_stmt') }}
    where period_type = 'annual'
)

select
    q.symbol,

    -- Latest quarter EPS/Revenue
    max(q.eps) filter (where q.q_rank = 1) as eps_q0,
    max(q.eps_yoy_base) filter (where q.q_rank = 1) as eps_q0_yoy_base,

    -- Quarterly YoY growth rates for acceleration check
    max(case when q.q_rank = 1 and q.eps_yoy_base > 0
        then q.eps / q.eps_yoy_base - 1 end) as eps_g0,
    max(case when q.q_rank = 2 and q.eps_yoy_base > 0
        then q.eps / q.eps_yoy_base - 1 end) as eps_g1,
    max(case when q.q_rank = 3 and q.eps_yoy_base > 0
        then q.eps / q.eps_yoy_base - 1 end) as eps_g2,

    max(q.revenue) filter (where q.q_rank = 1) as rev_q0,
    max(q.rev_yoy_base) filter (where q.q_rank = 1) as rev_q0_yoy_base,

    max(case when q.q_rank = 1 and q.rev_yoy_base > 0
        then q.revenue / q.rev_yoy_base - 1 end) as rev_g0,
    max(case when q.q_rank = 2 and q.rev_yoy_base > 0
        then q.revenue / q.rev_yoy_base - 1 end) as rev_g1,
    max(case when q.q_rank = 3 and q.rev_yoy_base > 0
        then q.revenue / q.rev_yoy_base - 1 end) as rev_g2,

    -- Annual data for 3Y CAGR + acceleration
    max(a.eps) filter (where a.fy_rank = 1) as eps_fy0,
    max(a.eps_fy_3y_ago) filter (where a.fy_rank = 1) as eps_fy3,
    max(a.revenue) filter (where a.fy_rank = 1) as rev_fy0,
    max(a.rev_fy_3y_ago) filter (where a.fy_rank = 1) as rev_fy3,

    -- Annual YoY for acceleration
    max(case when a.fy_rank = 1 and a.eps_fy_prev > 0
        then a.eps / a.eps_fy_prev - 1 end) as eps_fy_g0,
    max(case when a.fy_rank = 2 and a.eps_fy_prev > 0
        then a.eps / a.eps_fy_prev - 1 end) as eps_fy_g1,
    max(case when a.fy_rank = 1 and a.rev_fy_prev > 0
        then a.revenue / a.rev_fy_prev - 1 end) as rev_fy_g0,
    max(case when a.fy_rank = 2 and a.rev_fy_prev > 0
        then a.revenue / a.rev_fy_prev - 1 end) as rev_fy_g1

from quarterly_ranked q
left join annual_ranked a using (symbol)
where q.q_rank <= 4
group by q.symbol
