-- Dagster instance storage on Golden Source (schema ops_dagster).
-- Apply once (superuser or owner with CREATE privilege):
--   psql -d bifrost_golden_source -f scripts/ops_dagster_schema.sql
-- Dagster creates its own tables on first webserver/daemon start (search_path=ops_dagster).
-- Research API (analytics_writer) needs SELECT on runs for GET /research/orchestration/status.

CREATE SCHEMA IF NOT EXISTS ops_dagster;

-- Prefer applying as table owner (analytics_writer) or superuser.
-- GRANT ON ALL TABLES fails mid-loop if the current role does not own every table;
-- grant runs (+ USAGE) best-effort per role instead.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_writer') THEN
    GRANT USAGE, CREATE ON SCHEMA ops_dagster TO analytics_writer;
    ALTER DEFAULT PRIVILEGES IN SCHEMA ops_dagster
      GRANT ALL ON TABLES TO analytics_writer;
    BEGIN
      GRANT SELECT ON ALL TABLES IN SCHEMA ops_dagster TO analytics_writer;
    EXCEPTION WHEN insufficient_privilege OR undefined_table THEN
      NULL;
    END;
    BEGIN
      GRANT SELECT ON TABLE ops_dagster.runs TO analytics_writer;
    EXCEPTION WHEN insufficient_privilege OR undefined_table THEN
      NULL;
    END;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bifrost') THEN
    GRANT USAGE ON SCHEMA ops_dagster TO bifrost;
    ALTER DEFAULT PRIVILEGES IN SCHEMA ops_dagster
      GRANT SELECT ON TABLES TO bifrost;
    BEGIN
      GRANT SELECT ON ALL TABLES IN SCHEMA ops_dagster TO bifrost;
    EXCEPTION WHEN insufficient_privilege OR undefined_table THEN
      NULL;
    END;
    BEGIN
      GRANT SELECT ON TABLE ops_dagster.runs TO bifrost;
    EXCEPTION WHEN insufficient_privilege OR undefined_table THEN
      NULL;
    END;
  END IF;
END $$;
