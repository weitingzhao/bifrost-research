-- Wave 6.4 superuser fixup — move legacy features_* tables into canonical features.* names.
-- Run as CNPG postgres superuser when empty canonical shells blocked the standard migration.
-- Idempotent: skips when canonical already holds data or legacy is missing.

BEGIN;

CREATE SCHEMA IF NOT EXISTS features;

DO $$
DECLARE
    rec RECORD;
    legacy_reg regclass;
    canon_reg regclass;
    canon_cnt bigint;
BEGIN
    FOR rec IN
        SELECT * FROM (VALUES
            ('features_daily', 'atm_iv_daily', 'option_metric_atm_iv_daily'),
            ('features_daily', 'max_pain_daily', 'option_metric_max_pain_daily'),
            ('features_daily', 'pcr_daily', 'option_metric_pcr_daily'),
            ('features_daily', 'iv_percentile_daily', 'option_metric_iv_percentile_daily'),
            ('features_option', 'gex_daily', 'option_metric_gex_daily'),
            ('features_option', 'gex_intraday', 'option_metric_gex_intraday'),
            ('features_option', 'gex_levels_daily', 'option_metric_gex_levels_daily'),
            ('features_option', 'iv_surface_daily', 'option_surface_iv_daily'),
            ('features_option', 'order_sentiment_daily', 'option_flow_sentiment_daily'),
            ('features_option', 'multi_leg_trades', 'option_flow_multi_leg_daily'),
            ('features_signals', 'momentum_score_daily', 'stock_signal_momentum_daily'),
            ('features_signals', 'event_radar', 'event_signal_radar_daily'),
            ('features_forecasts', 'market_terrain_daily', 'stock_forecast_terrain_daily'),
            ('features_forecasts', 'terrain_intraday', 'stock_forecast_terrain_intraday'),
            ('features_forecasts', 'forecast_session', 'stock_forecast_session'),
            ('features_forecasts', 'forecast_hourly', 'stock_forecast_hourly'),
            ('features_backtests', 'forecast_settlement', 'stock_backtest_settlement'),
            ('features_backtests', 'backtest_results', 'stock_backtest_results_period')
        ) AS t(legacy_schema, old_name, new_name)
    LOOP
        legacy_reg := to_regclass(rec.legacy_schema || '.' || rec.old_name);
        canon_reg := to_regclass('features.' || rec.new_name);

        IF legacy_reg IS NULL THEN
            CONTINUE;
        END IF;

        IF canon_reg IS NOT NULL THEN
            EXECUTE format('SELECT count(*) FROM %s', canon_reg) INTO canon_cnt;
            IF canon_cnt = 0 THEN
                EXECUTE format('DROP TABLE %s CASCADE', canon_reg);
                canon_reg := NULL;
            ELSE
                -- Canonical already populated (e.g. stock_signal_sepa_daily) — keep canonical.
                CONTINUE;
            END IF;
        END IF;

        EXECUTE format(
            'ALTER TABLE %I.%I SET SCHEMA features',
            rec.legacy_schema,
            rec.old_name
        );
        IF to_regclass('features.' || rec.old_name) IS NOT NULL
           AND rec.old_name <> rec.new_name THEN
            EXECUTE format(
                'ALTER TABLE features.%I RENAME TO %I',
                rec.old_name,
                rec.new_name
            );
        END IF;
    END LOOP;
END $$;

-- SEPA: projection mart owns features.stock_signal_sepa_daily; drop stale legacy table only.
DROP TABLE IF EXISTS features_signals.sepa_score_daily;

-- Ensure writers retain access on moved tables.
GRANT USAGE ON SCHEMA features TO bifrost;
GRANT USAGE ON SCHEMA features TO analytics_writer;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA features TO bifrost;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA features TO analytics_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA features GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO bifrost;
ALTER DEFAULT PRIVILEGES IN SCHEMA features GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO analytics_writer;

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'features'
    LOOP
        EXECUTE format('ALTER TABLE features.%I OWNER TO bifrost', r.tablename);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON features.%I TO bifrost', r.tablename);
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON features.%I TO analytics_writer',
            r.tablename
        );
    END LOOP;
END $$;

COMMIT;
