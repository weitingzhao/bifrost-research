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
    ("market_analytics", "features_daily", None),
    ("analytics_elementary", "ops_dbt", None),
    ("data_ops", "ops_jobs", {"job_ingest", "ingest_freshness"}),
    ("flex_ops", "ops_jobs", {"job_flex_ingest", "flex_ingest_freshness"}),
]

RESEARCH_VIEWS: list[tuple[str, str]] = [
    ("gex_daily", "features_option.gex_daily"),
    ("gex_levels_daily", "features_option.gex_levels_daily"),
    ("gex_intraday", "features_option.gex_intraday"),
    ("iv_surface_daily", "features_option.iv_surface_daily"),
    ("order_sentiment_daily", "features_option.order_sentiment_daily"),
    ("multi_leg_trades", "features_option.multi_leg_trades"),
    ("momentum_score_daily", "signals.momentum_score_daily"),
    ("sepa_score_daily", "signals.sepa_score_daily"),
    ("event_radar", "signals.event_radar"),
    ("market_terrain_daily", "forecasts.market_terrain_daily"),
    ("terrain_intraday", "forecasts.terrain_intraday"),
    ("forecast_session", "forecasts.forecast_session"),
    ("forecast_hourly", "forecasts.forecast_hourly"),
    ("forecast_settlement", "backtests.forecast_settlement"),
    ("backtest_results", "backtests.backtest_results"),
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
    conn = connect()
    with conn.cursor() as cur:
        for schema in LEGACY_SCHEMAS:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

        for old_schema, new_schema, only_tables in MAPPINGS:
            cur.execute(
                """
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind IN ('r','p','v','m')
                ORDER BY 1
                """,
                (new_schema,),
            )
            for (relname,) in cur.fetchall():
                if only_tables is not None and relname not in only_tables:
                    continue
                cur.execute(
                    f"CREATE OR REPLACE VIEW {old_schema}.{relname} AS SELECT * FROM {new_schema}.{relname}"
                )
                print(f"view {old_schema}.{relname}")

        cur.execute("CREATE SCHEMA IF NOT EXISTS research")
        for view_name, target in RESEARCH_VIEWS:
            cur.execute(
                f"CREATE OR REPLACE VIEW research.{view_name} AS SELECT * FROM {target}"
            )
            print(f"view research.{view_name}")

        cur.execute("CREATE SCHEMA IF NOT EXISTS analytics")
        for alias, target in SEPA_ALIASES:
            cur.execute(
                f"CREATE OR REPLACE VIEW analytics.{alias} AS SELECT * FROM {target}"
            )
            print(f"view analytics.{alias}")

        for schema in LEGACY_SCHEMAS:
            cur.execute(
                f"GRANT USAGE ON SCHEMA {schema} TO analytics_writer, analytics_reader, bifrost"
            )
            cur.execute(
                f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} "
                "TO analytics_writer, analytics_reader, bifrost"
            )
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
