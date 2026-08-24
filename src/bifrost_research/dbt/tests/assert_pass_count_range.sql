/*
  Generic test: assert that a pass_count column is within expected range.
  Usage in schema.yml:
    tests:
      - assert_pass_count_range:
          model: ref('mart_sepa_fundamental_eval')
          column: pass_count
          min_value: 0
          max_value: 8
*/

{% test assert_pass_count_range(model, column_name, min_value, max_value) %}

select {{ column_name }}
from {{ model }}
where {{ column_name }} < {{ min_value }}
   or {{ column_name }} > {{ max_value }}

{% endtest %}
