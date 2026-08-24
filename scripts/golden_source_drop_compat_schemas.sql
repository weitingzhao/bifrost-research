-- Wave 5 cleanup: drop legacy compatibility schemas (views only).
-- Run as CNPG postgres superuser after consumers use pipeline schema names.

DROP SCHEMA IF EXISTS market CASCADE;
DROP SCHEMA IF EXISTS brokerage CASCADE;
DROP SCHEMA IF EXISTS analytics CASCADE;
DROP SCHEMA IF EXISTS market_analytics CASCADE;
DROP SCHEMA IF EXISTS research CASCADE;
DROP SCHEMA IF EXISTS data_ops CASCADE;
DROP SCHEMA IF EXISTS flex_ops CASCADE;
DROP SCHEMA IF EXISTS analytics_elementary CASCADE;
