-- Golden Source schema pipeline migration (one-shot — DO NOT RE-RUN).
-- Historical: created signals/forecasts/backtests from research split.
-- Post-2026-08 rename: canonical names are features_signals/features_forecasts/features_backtests
--   (see scripts/rename_schemas_to_features_prefix.sql + drop_legacy_feature_schemas.sql).
-- Re-running this script after rename will recreate retired schema names.

BEGIN;

-- 1.1 Simple renames
ALTER SCHEMA market RENAME TO raw_market;
ALTER SCHEMA brokerage RENAME TO raw_broker;
ALTER SCHEMA analytics RENAME TO dw_stock;
ALTER SCHEMA market_analytics RENAME TO features_daily;
ALTER SCHEMA analytics_elementary RENAME TO ops_dbt;

-- 1.2 Merge data_ops + flex_ops -> ops_jobs (rename flex table + PK before move)
CREATE SCHEMA ops_jobs;
ALTER TABLE flex_ops.ingest_freshness RENAME TO flex_ingest_freshness;
ALTER TABLE flex_ops.flex_ingest_freshness RENAME CONSTRAINT ingest_freshness_pkey TO flex_ingest_freshness_pkey;
ALTER TABLE data_ops.job_ingest SET SCHEMA ops_jobs;
ALTER TABLE data_ops.ingest_freshness SET SCHEMA ops_jobs;
ALTER TABLE flex_ops.job_flex_ingest SET SCHEMA ops_jobs;
ALTER TABLE flex_ops.flex_ingest_freshness SET SCHEMA ops_jobs;
ALTER FUNCTION data_ops.ensure_month_partitions(text, text, integer, integer) SET SCHEMA ops_jobs;
ALTER FUNCTION data_ops.ensure_year_partitions(text, text, integer, integer) SET SCHEMA ops_jobs;
ALTER FUNCTION data_ops.ensure_day_partitions(text, text, integer, integer) SET SCHEMA ops_jobs;
ALTER FUNCTION data_ops.drop_day_partitions_older_than(text, text, integer) SET SCHEMA ops_jobs;
DROP SCHEMA data_ops;
DROP SCHEMA flex_ops;

-- 1.3 Split research (historical — executed before features_* rename)
-- Post-rename canonical names: features_signals, features_forecasts, features_backtests
-- See scripts/rename_schemas_to_features_prefix.sql
CREATE SCHEMA features_option;
CREATE SCHEMA signals;
CREATE SCHEMA forecasts;
CREATE SCHEMA backtests;

ALTER TABLE research.gex_daily SET SCHEMA features_option;
ALTER TABLE research.gex_levels_daily SET SCHEMA features_option;
ALTER TABLE research.gex_intraday SET SCHEMA features_option;
ALTER TABLE research.iv_surface_daily SET SCHEMA features_option;
ALTER TABLE research.order_sentiment_daily SET SCHEMA features_option;
ALTER TABLE research.multi_leg_trades SET SCHEMA features_option;

ALTER TABLE research.momentum_score_daily SET SCHEMA signals;
ALTER TABLE research.sepa_score_daily SET SCHEMA signals;
ALTER TABLE research.event_radar SET SCHEMA signals;

ALTER TABLE research.market_terrain_daily SET SCHEMA forecasts;
ALTER TABLE research.terrain_intraday SET SCHEMA forecasts;
ALTER TABLE research.forecast_session SET SCHEMA forecasts;
ALTER TABLE research.forecast_hourly SET SCHEMA forecasts;

ALTER TABLE research.forecast_settlement SET SCHEMA backtests;
ALTER TABLE research.backtest_results SET SCHEMA backtests;

DROP SCHEMA research;

COMMIT;
