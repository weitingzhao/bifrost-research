{{ config(materialized='table') }}

select
    u.symbol,
    current_date as eval_date,

    -- Condition 1: EPS quarterly YoY ≥ 25%
    coalesce(f.eps_g0 >= 0.25, false) as eps_q2q_ge_25pct,

    -- Condition 2: Revenue quarterly YoY ≥ 25%
    coalesce(f.rev_g0 >= 0.25, false) as rev_q2q_ge_25pct,

    -- Condition 3: EPS acceleration (3 consecutive QoQ growths increasing)
    coalesce(f.eps_g0 > f.eps_g1 and f.eps_g1 > f.eps_g2, false) as eps_acc_2q,

    -- Condition 4: Revenue acceleration
    coalesce(f.rev_g0 > f.rev_g1 and f.rev_g1 > f.rev_g2, false) as rev_acc_2q,

    -- Condition 5: EPS 3Y CAGR ≥ 15%
    coalesce(
        case
            when f.eps_fy0 > 0 and f.eps_fy3 > 0
            then power(f.eps_fy0 / f.eps_fy3, 1.0 / 3) - 1 >= 0.15
            else false
        end,
        false
    ) as eps_3y_ge_15pct,

    -- Condition 6: Revenue 3Y CAGR ≥ 15%
    coalesce(
        case
            when f.rev_fy0 > 0 and f.rev_fy3 > 0
            then power(f.rev_fy0 / f.rev_fy3, 1.0 / 3) - 1 >= 0.15
            else false
        end,
        false
    ) as rev_3y_ge_15pct,

    -- Condition 7: Annual EPS acceleration
    coalesce(f.eps_fy_g0 > f.eps_fy_g1, false) as eps_acc_fy,

    -- Condition 8: Annual Revenue acceleration
    coalesce(f.rev_fy_g0 > f.rev_fy_g1, false) as rev_acc_fy,

    -- Pass count summary
    (
        coalesce(f.eps_g0 >= 0.25, false)::int
        + coalesce(f.rev_g0 >= 0.25, false)::int
        + coalesce(f.eps_g0 > f.eps_g1 and f.eps_g1 > f.eps_g2, false)::int
        + coalesce(f.rev_g0 > f.rev_g1 and f.rev_g1 > f.rev_g2, false)::int
        + coalesce(case when f.eps_fy0 > 0 and f.eps_fy3 > 0
            then power(f.eps_fy0 / f.eps_fy3, 1.0 / 3) - 1 >= 0.15
            else false end, false)::int
        + coalesce(case when f.rev_fy0 > 0 and f.rev_fy3 > 0
            then power(f.rev_fy0 / f.rev_fy3, 1.0 / 3) - 1 >= 0.15
            else false end, false)::int
        + coalesce(f.eps_fy_g0 > f.eps_fy_g1, false)::int
        + coalesce(f.rev_fy_g0 > f.rev_fy_g1, false)::int
    ) as pass_count,

    -- Data sufficiency flag
    (f.eps_q0_yoy_base is null) as insufficient_data,

    -- Raw metrics for inspector
    f.eps_q0,
    f.eps_q0_yoy_base,
    f.eps_g0,
    f.eps_g1,
    f.eps_g2,
    f.rev_q0,
    f.rev_q0_yoy_base,
    f.rev_g0,
    f.rev_g1,
    f.rev_g2,
    f.eps_fy0,
    f.eps_fy3,
    f.rev_fy0,
    f.rev_fy3,
    f.eps_fy_g0,
    f.eps_fy_g1,
    f.rev_fy_g0,
    f.rev_fy_g1

from {{ ref('dim_universe') }} u
left join {{ ref('int_financials_yoy') }} f on u.symbol = f.symbol
where u.included = true
