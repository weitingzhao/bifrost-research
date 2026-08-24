-- Create pipeline schemas and roles for bifrost-research dbt project
-- Run against bifrost_golden_source database with a superuser/owner role

CREATE SCHEMA IF NOT EXISTS dw_stock;

-- Writer role: used by dbt to materialize models
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analytics_writer') THEN
        CREATE ROLE analytics_writer WITH LOGIN PASSWORD 'changeme';
    END IF;
END
$$;

-- Reader role: used by Trade API pods to query analytics tables
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analytics_reader') THEN
        CREATE ROLE analytics_reader WITH NOLOGIN;
    END IF;
END
$$;

-- Grant read access to raw_market schema (source data)
GRANT USAGE ON SCHEMA raw_market TO analytics_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA raw_market TO analytics_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA raw_market GRANT SELECT ON TABLES TO analytics_writer;

-- Grant write access to dw_stock schema (dbt output)
GRANT USAGE, CREATE ON SCHEMA dw_stock TO analytics_writer;

-- Grant read access to dw_stock for consumers
GRANT USAGE ON SCHEMA dw_stock TO analytics_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA dw_stock
    GRANT SELECT ON TABLES TO analytics_reader;

-- ---------------------------------------------------------------------------
-- Elementary observability schema (dbt package + edr CLI)
-- With profile schema=dw_stock and models.elementary.+schema=ops_dbt,
-- dbt materializes into ops_dbt.
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS ops_dbt;

GRANT USAGE, CREATE ON SCHEMA ops_dbt TO analytics_writer;
GRANT USAGE ON SCHEMA ops_dbt TO analytics_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_writer IN SCHEMA ops_dbt
    GRANT SELECT ON TABLES TO analytics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA ops_dbt TO analytics_reader;
