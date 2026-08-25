-- Wave 6.6 — drop legacy features_* view schemas after Wave 6.5 consumer cutover.
-- Idempotent: DROP SCHEMA IF EXISTS CASCADE.

BEGIN;

DROP SCHEMA IF EXISTS features_daily CASCADE;
DROP SCHEMA IF EXISTS features_option CASCADE;
DROP SCHEMA IF EXISTS features_signals CASCADE;
DROP SCHEMA IF EXISTS features_forecasts CASCADE;
DROP SCHEMA IF EXISTS features_backtests CASCADE;

COMMIT;
