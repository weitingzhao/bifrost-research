"""Canonical Golden Source schema names for Research-owned feature tables."""

SCHEMA_FEATURES_OPTION = "features_option"
SCHEMA_FEATURES_SIGNALS = "features_signals"
SCHEMA_FEATURES_FORECASTS = "features_forecasts"
SCHEMA_FEATURES_BACKTESTS = "features_backtests"

# Qualified table names (schema.table)
TABLE_MOMENTUM_SCORE_DAILY = f"{SCHEMA_FEATURES_SIGNALS}.momentum_score_daily"
TABLE_SEPA_SCORE_DAILY = f"{SCHEMA_FEATURES_SIGNALS}.sepa_score_daily"
TABLE_EVENT_RADAR = f"{SCHEMA_FEATURES_SIGNALS}.event_radar"
TABLE_MARKET_TERRAIN_DAILY = f"{SCHEMA_FEATURES_FORECASTS}.market_terrain_daily"
TABLE_TERRAIN_INTRADAY = f"{SCHEMA_FEATURES_FORECASTS}.terrain_intraday"
TABLE_FORECAST_SESSION = f"{SCHEMA_FEATURES_FORECASTS}.forecast_session"
TABLE_FORECAST_HOURLY = f"{SCHEMA_FEATURES_FORECASTS}.forecast_hourly"
TABLE_FORECAST_SETTLEMENT = f"{SCHEMA_FEATURES_BACKTESTS}.forecast_settlement"
TABLE_BACKTEST_RESULTS = f"{SCHEMA_FEATURES_BACKTESTS}.backtest_results"
