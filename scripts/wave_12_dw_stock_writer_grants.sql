-- Wave 12: analytics_writer must refresh incremental dw_stock models owned by bifrost.
-- Run as postgres superuser on bifrost_golden_source.

GRANT USAGE, CREATE ON SCHEMA dw_stock TO analytics_writer;

GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA dw_stock TO analytics_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA dw_stock TO analytics_writer;

ALTER DEFAULT PRIVILEGES FOR ROLE bifrost IN SCHEMA dw_stock
  GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO analytics_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA dw_stock
  GRANT SELECT ON TABLES TO analytics_reader, bifrost;

-- dbt runs as analytics_writer; bifrost-owned tables block CREATE OR REPLACE.
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'dw_stock'
  LOOP
    EXECUTE format('ALTER TABLE dw_stock.%I OWNER TO analytics_writer', r.tablename);
  END LOOP;
END $$;
