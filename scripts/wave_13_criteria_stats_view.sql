-- Wave 13: mart_sepa_criteria_stats table → dbt view (O3).
-- Idempotent pre-migration before first dbt run with materialized='view'.
DROP TABLE IF EXISTS dw_stock.mart_sepa_criteria_stats CASCADE;
