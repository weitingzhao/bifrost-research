/*
  A pass_count column stays inside [min_value, max_value]. Returns the offending rows.

  Until 2026-10-06 (TD-111) this block sat in tests/ — dbt's singular-test path —
  so it was never applied and errored whenever a selection picked it up
  (14 error rows in ops_dbt.dbt_run_results, 08-21..09-28, never a pass).
  Applied in models/marts/_marts__models.yml to the fundamental (0-8) and
  technical (0-11) evaluations.
*/

{% test assert_pass_count_range(model, column_name, min_value, max_value) %}

select {{ column_name }}
from {{ model }}
where {{ column_name }} is null
   or {{ column_name }} < {{ min_value }}
   or {{ column_name }} > {{ max_value }}

{% endtest %}
