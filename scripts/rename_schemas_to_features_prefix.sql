-- Rename signals / forecasts / backtests → features_* prefix (Golden Source).
-- Instant metadata operation; run once after deploying bifrost-research with new DDL.
-- Must run as schema owner (postgres superuser on CNPG), not analytics_writer.
-- Rollback: ALTER SCHEMA features_signals RENAME TO signals; etc.

BEGIN;

ALTER SCHEMA signals RENAME TO features_signals;
ALTER SCHEMA forecasts RENAME TO features_forecasts;
ALTER SCHEMA backtests RENAME TO features_backtests;

ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA features_signals
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA features_forecasts
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA features_backtests
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;

COMMIT;
