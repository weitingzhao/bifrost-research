-- Wave 6.4 — unify features_* schemas into single features schema with four-part names.
-- Idempotent: safe to re-run. Run against bifrost_golden_source.
-- After migration, legacy locations are read-only views.

BEGIN;

CREATE SCHEMA IF NOT EXISTS features;

-- Helper: move table from legacy schema to features with new name, then create compat view.
DO $$
DECLARE
    rec RECORD;
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
            ('features_signals', 'sepa_score_daily', 'stock_signal_sepa_daily'),
            ('features_signals', 'event_radar', 'event_signal_radar_daily'),
            ('features_forecasts', 'market_terrain_daily', 'stock_forecast_terrain_daily'),
            ('features_forecasts', 'terrain_intraday', 'stock_forecast_terrain_intraday'),
            ('features_forecasts', 'forecast_session', 'stock_forecast_session'),
            ('features_forecasts', 'forecast_hourly', 'stock_forecast_hourly'),
            ('features_backtests', 'forecast_settlement', 'stock_backtest_settlement'),
            ('features_backtests', 'backtest_results', 'stock_backtest_results_period')
        ) AS t(legacy_schema, old_name, new_name)
    LOOP
        IF to_regclass(rec.legacy_schema || '.' || rec.old_name) IS NOT NULL
           AND to_regclass('features.' || rec.new_name) IS NULL THEN
            EXECUTE format(
                'ALTER TABLE %I.%I SET SCHEMA features',
                rec.legacy_schema,
                rec.old_name
            );
            IF to_regclass('features.' || rec.old_name) IS NOT NULL THEN
                EXECUTE format(
                    'ALTER TABLE features.%I RENAME TO %I',
                    rec.old_name,
                    rec.new_name
                );
            END IF;
        END IF;
    END LOOP;
END $$;

-- Add asof_ts to SEPA projection table if migrated from legacy
ALTER TABLE IF EXISTS features.stock_signal_sepa_daily
    ADD COLUMN IF NOT EXISTS asof_ts timestamptz;

-- Legacy compat views (read-only)
CREATE SCHEMA IF NOT EXISTS features_daily;
CREATE SCHEMA IF NOT EXISTS features_option;
CREATE SCHEMA IF NOT EXISTS features_signals;
CREATE SCHEMA IF NOT EXISTS features_forecasts;
CREATE SCHEMA IF NOT EXISTS features_backtests;

CREATE OR REPLACE VIEW features_daily.atm_iv_daily AS
    SELECT * FROM features.option_metric_atm_iv_daily;
CREATE OR REPLACE VIEW features_daily.max_pain_daily AS
    SELECT * FROM features.option_metric_max_pain_daily;
CREATE OR REPLACE VIEW features_daily.pcr_daily AS
    SELECT * FROM features.option_metric_pcr_daily;
CREATE OR REPLACE VIEW features_daily.iv_percentile_daily AS
    SELECT * FROM features.option_metric_iv_percentile_daily;

CREATE OR REPLACE VIEW features_option.gex_daily AS
    SELECT * FROM features.option_metric_gex_daily;
CREATE OR REPLACE VIEW features_option.gex_intraday AS
    SELECT * FROM features.option_metric_gex_intraday;
CREATE OR REPLACE VIEW features_option.gex_levels_daily AS
    SELECT * FROM features.option_metric_gex_levels_daily;
CREATE OR REPLACE VIEW features_option.iv_surface_daily AS
    SELECT * FROM features.option_surface_iv_daily;
CREATE OR REPLACE VIEW features_option.order_sentiment_daily AS
    SELECT * FROM features.option_flow_sentiment_daily;
CREATE OR REPLACE VIEW features_option.multi_leg_trades AS
    SELECT * FROM features.option_flow_multi_leg_daily;

CREATE OR REPLACE VIEW features_signals.momentum_score_daily AS
    SELECT * FROM features.stock_signal_momentum_daily;
CREATE OR REPLACE VIEW features_signals.sepa_score_daily AS
    SELECT * FROM features.stock_signal_sepa_daily;
CREATE OR REPLACE VIEW features_signals.event_radar AS
    SELECT * FROM features.event_signal_radar_daily;

CREATE OR REPLACE VIEW features_forecasts.market_terrain_daily AS
    SELECT * FROM features.stock_forecast_terrain_daily;
CREATE OR REPLACE VIEW features_forecasts.terrain_intraday AS
    SELECT * FROM features.stock_forecast_terrain_intraday;
CREATE OR REPLACE VIEW features_forecasts.forecast_session AS
    SELECT * FROM features.stock_forecast_session;
CREATE OR REPLACE VIEW features_forecasts.forecast_hourly AS
    SELECT * FROM features.stock_forecast_hourly;

CREATE OR REPLACE VIEW features_backtests.forecast_settlement AS
    SELECT * FROM features.stock_backtest_settlement;
CREATE OR REPLACE VIEW features_backtests.backtest_results AS
    SELECT * FROM features.stock_backtest_results_period;

COMMIT;
