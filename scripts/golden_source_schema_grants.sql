-- Grants for pipeline schemas after migration.
GRANT USAGE, CREATE ON SCHEMA raw_market, raw_broker, dw_stock,
    features_daily, features_option, features_signals, features_forecasts, features_backtests,
    ops_jobs, ops_dbt TO analytics_writer;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA dw_stock TO analytics_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA dw_stock TO analytics_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE bifrost IN SCHEMA dw_stock
  GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO analytics_writer;
GRANT USAGE ON SCHEMA raw_market, raw_broker, dw_stock,
    features_daily, features_option, features_signals, features_forecasts, features_backtests,
    ops_jobs, ops_dbt TO analytics_reader, bifrost;
GRANT SELECT ON ALL TABLES IN SCHEMA raw_market, raw_broker, dw_stock,
    features_daily, features_option, features_signals, features_forecasts, features_backtests,
    ops_jobs, ops_dbt TO analytics_reader, bifrost;
GRANT INSERT, UPDATE, DELETE ON ops_jobs.job_flex_ingest TO bifrost;
GRANT INSERT, UPDATE ON ops_jobs.flex_ingest_freshness TO bifrost;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops_jobs TO bifrost;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA raw_market
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA raw_broker
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA dw_stock
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA features_daily
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA features_option
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA features_signals
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA features_forecasts
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA features_backtests
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA ops_jobs
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA ops_dbt
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;
