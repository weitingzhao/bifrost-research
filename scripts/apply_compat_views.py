#!/usr/bin/env python3
"""Generate and apply legacy-schema compatibility views after pipeline migration.

DEPRECATED for production Golden Source after Wave 5 cleanup (2026-08).
Running this recreates empty legacy schema namespaces. Use canonical names
(dw_stock, raw_market, raw_broker, ops_dbt) only.
"""
from __future__ import annotations

import os
import sys

import psycopg

MAPPINGS: list[tuple[str, str, set[str] | None]] = [
    ("market", "raw_market", None),
    ("brokerage", "raw_broker", None),
    ("analytics", "dw_stock", None),
    ("market_analytics", "features", None),
    ("analytics_elementary", "ops_dbt", None),
    ("data_ops", "ops_jobs", {"job_ingest", "ingest_freshness"}),
    ("flex_ops", "ops_jobs", {"job_flex_ingest", "flex_ingest_freshness"}),
]

RESEARCH_VIEWS: list[tuple[str, str]] = [
    ("gex_daily", "features.option_metric_gex_daily"),
    ("gex_levels_daily", "features.option_metric_gex_levels_daily"),
    ("gex_intraday", "features.option_metric_gex_intraday"),
    ("iv_surface_daily", "features.option_surface_iv_daily"),
    ("order_sentiment_daily", "features.option_flow_sentiment_daily"),
    ("multi_leg_trades", "features.option_flow_multi_leg_daily"),
    ("momentum_score_daily", "features.stock_signal_momentum_daily"),
    ("sepa_score_daily", "features.stock_signal_sepa_daily"),
    ("event_radar", "features.event_signal_radar_daily"),
    ("market_terrain_daily", "features.stock_forecast_terrain_daily"),
    ("terrain_intraday", "features.stock_forecast_terrain_intraday"),
    ("forecast_session", "features.stock_forecast_session"),
    ("forecast_hourly", "features.stock_forecast_hourly"),
    ("forecast_settlement", "features.stock_backtest_settlement"),
    ("backtest_results", "features.stock_backtest_results_period"),
]

SEPA_ALIASES: list[tuple[str, str]] = [
    ("sepa_criteria_stats", "dw_stock.mart_sepa_criteria_stats"),
    ("sepa_fundamental_eval", "dw_stock.mart_sepa_fundamental_eval"),
    ("sepa_technical_eval", "dw_stock.mart_sepa_technical_eval"),
    ("sepa_fundamental_ext", "dw_stock.mart_sepa_fundamental_ext"),
    ("sepa_screener_wide", "dw_stock.mart_sepa_screener_wide"),
    ("sepa_screening_ranked", "dw_stock.mart_sepa_screening_ranked"),
    ("sepa_composite_score", "dw_stock.mart_sepa_composite_score"),
    ("sepa_tier_momentum", "dw_stock.mart_sepa_tier_momentum"),
    ("sepa_tier_sentiment", "dw_stock.mart_sepa_tier_sentiment"),
    ("sepa_tier_structure", "dw_stock.mart_sepa_tier_structure"),
]

LEGACY_SCHEMAS = [
    "market",
    "brokerage",
    "analytics",
    "market_analytics",
    "research",
    "data_ops",
    "flex_ops",
    "analytics_elementary",
    # Retired after features_* rename — never recreate compat views for these.
    "signals",
    "forecasts",
    "backtests",
]


def connect() -> psycopg.Connection:
    password = os.environ.get("POSTGRES_PASSWORD") or os.environ.get("PGPASSWORD")
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "192.168.10.73"),
        port=int(os.environ.get("POSTGRES_PORT", "30432")),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=password,
        dbname=os.environ.get("POSTGRES_DB", "bifrost_golden_source"),
        autocommit=True,
    )


def main() -> int:
    print(
        "apply_compat_views is DEPRECATED and REFUSED after Golden Source pipeline cleanup.\n"
        "Do not run — it recreates legacy schema namespaces (market/analytics/research/...).\n"
        "Canonical names: raw_market, dw_stock, features.*.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
