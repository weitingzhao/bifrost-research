{{ config(materialized='view') }}

/*
  Pre-aggregated per-condition pass/fail statistics.
  Stored as domain + jsonb stats to avoid UNION ALL column mismatch
  between fundamental (8 conditions) and technical (11 conditions).
  The API reads by domain and unpacks the stats object.
*/

with fund_stats as (
    select
        count(*) as total,
        count(*) filter (where not insufficient_data) as evaluated,
        count(*) filter (where insufficient_data) as no_data,
        count(*) filter (where eps_q2q_ge_25pct) as eps_q2q_pass,
        count(*) filter (where not eps_q2q_ge_25pct and not insufficient_data) as eps_q2q_fail,
        count(*) filter (where rev_q2q_ge_25pct) as rev_q2q_pass,
        count(*) filter (where not rev_q2q_ge_25pct and not insufficient_data) as rev_q2q_fail,
        count(*) filter (where eps_acc_2q) as eps_acc_pass,
        count(*) filter (where not eps_acc_2q and not insufficient_data) as eps_acc_fail,
        count(*) filter (where rev_acc_2q) as rev_acc_pass,
        count(*) filter (where not rev_acc_2q and not insufficient_data) as rev_acc_fail,
        count(*) filter (where eps_3y_ge_15pct) as eps_3y_pass,
        count(*) filter (where not eps_3y_ge_15pct and not insufficient_data) as eps_3y_fail,
        count(*) filter (where rev_3y_ge_15pct) as rev_3y_pass,
        count(*) filter (where not rev_3y_ge_15pct and not insufficient_data) as rev_3y_fail,
        count(*) filter (where eps_acc_fy) as eps_acc_fy_pass,
        count(*) filter (where not eps_acc_fy and not insufficient_data) as eps_acc_fy_fail,
        count(*) filter (where rev_acc_fy) as rev_acc_fy_pass,
        count(*) filter (where not rev_acc_fy and not insufficient_data) as rev_acc_fy_fail,
        count(*) filter (where pass_count = 8) as all_pass,
        count(*) filter (where pass_count >= 6) as pass_6_plus,
        count(*) filter (where pass_count >= 4) as pass_4_plus
    from {{ ref('mart_sepa_fundamental_eval') }}
),

tech_stats as (
    select
        count(*) as total,
        count(*) as evaluated,
        count(*) filter (where avg_volume_50_gt_threshold) as volume_pass,
        count(*) filter (where not avg_volume_50_gt_threshold) as volume_fail,
        count(*) filter (where close_ge_low52_x_1_3) as low52_pass,
        count(*) filter (where not close_ge_low52_x_1_3) as low52_fail,
        count(*) filter (where close_ge_high52_x_0_75) as high52_pass,
        count(*) filter (where not close_ge_high52_x_0_75) as high52_fail,
        count(*) filter (where sma50_gt_sma150) as sma50_150_pass,
        count(*) filter (where not sma50_gt_sma150) as sma50_150_fail,
        count(*) filter (where sma50_gt_sma200) as sma50_200_pass,
        count(*) filter (where not sma50_gt_sma200) as sma50_200_fail,
        count(*) filter (where sma150_gt_sma200) as sma150_200_pass,
        count(*) filter (where not sma150_gt_sma200) as sma150_200_fail,
        count(*) filter (where sma200_rising_1m) as sma200_rising_pass,
        count(*) filter (where not sma200_rising_1m) as sma200_rising_fail,
        count(*) filter (where price_gt_sma50) as price_sma50_pass,
        count(*) filter (where not price_gt_sma50) as price_sma50_fail,
        count(*) filter (where price_gt_sma150) as price_sma150_pass,
        count(*) filter (where not price_gt_sma150) as price_sma150_fail,
        count(*) filter (where price_gt_sma200) as price_sma200_pass,
        count(*) filter (where not price_gt_sma200) as price_sma200_fail,
        count(*) filter (where crs_ge_70) as crs_pass,
        count(*) filter (where not crs_ge_70) as crs_fail,
        count(*) filter (where pass_count = 11) as all_pass,
        count(*) filter (where pass_count >= 8) as pass_8_plus,
        count(*) filter (where pass_count >= 4) as pass_4_plus
    from {{ ref('mart_sepa_technical_eval') }}
)

select
    'fundamental' as domain,
    to_jsonb(f.*) as stats
from fund_stats f

union all

select
    'technical' as domain,
    to_jsonb(t.*) as stats
from tech_stats t
