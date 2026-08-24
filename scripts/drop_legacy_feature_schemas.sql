-- Drop retired bare feature schema names if they reappear (post features_* rename).
-- Safe after rename_schemas_to_features_prefix.sql; run as postgres superuser.
-- Idempotent: no-op when schemas already absent.

DROP SCHEMA IF EXISTS signals CASCADE;
DROP SCHEMA IF EXISTS forecasts CASCADE;
DROP SCHEMA IF EXISTS backtests CASCADE;
