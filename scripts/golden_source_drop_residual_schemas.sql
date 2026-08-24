-- Golden Source schema residual cleanup (post generate_schema_name macro fix)
-- Run only after dbt 0.5.6 verify job confirms Elementary writes to ops_dbt.

DROP SCHEMA IF EXISTS flex_ops CASCADE;
DROP SCHEMA IF EXISTS analytics CASCADE;
DROP SCHEMA IF EXISTS analytics_elementary CASCADE;
DROP SCHEMA IF EXISTS dw_stock_ops_dbt CASCADE;
