"""Idempotent DDL for features_daily.* and research.* (Research-owned).

Plugin may keep a thin re-export / shim for ``db-init`` compatibility, but
Research is the source of truth for these tables going forward.
"""

from __future__ import annotations

from typing import Any, Protocol

from bifrost_research.schema.schemas import (
    SCHEMA_FEATURES_BACKTESTS,
    SCHEMA_FEATURES_FORECASTS,
    SCHEMA_FEATURES_OPTION,
    SCHEMA_FEATURES_SIGNALS,
)


class _Cursor(Protocol):
    def execute(self, query: str, params: Any = None) -> Any: ...


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


MARKET_ANALYTICS_TABLES = (
    "max_pain_daily",
    "atm_iv_daily",
    "pcr_daily",
    "iv_percentile_daily",
)

RESEARCH_TABLES = (
    "momentum_score_daily",
    "gex_daily",
    "gex_levels_daily",
    "iv_surface_daily",
    "order_sentiment_daily",
    "multi_leg_trades",
    # Wave 4 — AI Intelligence
    "market_terrain_daily",
    "forecast_session",
    "forecast_hourly",
    "event_radar",
    "backtest_results",
    "forecast_settlement",
    # Wave 6 — Intraday (Product Parity)
    "terrain_intraday",
    "gex_intraday",
    # Wave B — SEPA fusion (Fund + Trend Template + Momentum + Options Structure)
    "sepa_score_daily",
)


# Retired Golden Source schema names — must not be recreated by DDL or compat scripts.
LEGACY_FEATURE_SCHEMAS = ("signals", "forecasts", "backtests")


def _drop_legacy_feature_schemas(cur: _Cursor) -> None:
    """Drop retired bare schema names if they reappear (empty or stale compat)."""
    for schema in LEGACY_FEATURE_SCHEMAS:
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,))
        if cur.fetchone():
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def apply_features_daily_ddl(conn: _Connection) -> None:
    """Create features_daily schema + four daily tables (idempotent)."""
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS features_daily")
        _create_features_daily_tables(cur)
    conn.commit()


def apply_research_ddl(conn: _Connection) -> None:
    """Create research schema + Wave 3–4 engine tables (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_FEATURES_OPTION}; "
            f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_FEATURES_SIGNALS}; "
            f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_FEATURES_FORECASTS}; "
            f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_FEATURES_BACKTESTS}"
        )
        _create_research_tables(cur)
        _drop_legacy_feature_schemas(cur)
    conn.commit()


def apply_all_ddl(conn: _Connection) -> None:
    """Apply features_daily + research DDL."""
    apply_features_daily_ddl(conn)
    apply_research_ddl(conn)


# Legacy Makefile / import alias (market_analytics → features_daily).
apply_market_analytics_ddl = apply_features_daily_ddl


def _create_features_daily_tables(cur: _Cursor) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_daily.max_pain_daily (
            symbol                 text        NOT NULL,
            trade_date             date        NOT NULL,
            expiry                 date        NOT NULL,
            max_pain_strike        double precision,
            total_oi               integer,
            total_pain_at_strike   double precision,
            computed_at            timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date, expiry)
        ) PARTITION BY RANGE (trade_date)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS max_pain_daily_symbol_date
        ON features_daily.max_pain_daily (symbol, trade_date DESC)
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_daily.atm_iv_daily (
            symbol             text        NOT NULL,
            trade_date         date        NOT NULL,
            expiry             date        NOT NULL,
            atm_strike         double precision,
            atm_iv             double precision,
            underlying_price   double precision,
            iv_source          text,
            computed_at        timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date, expiry)
        ) PARTITION BY RANGE (trade_date)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS atm_iv_daily_symbol_date
        ON features_daily.atm_iv_daily (symbol, trade_date DESC)
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_daily.pcr_daily (
            symbol              text        NOT NULL,
            trade_date          date        NOT NULL,
            pcr_oi              double precision,
            pcr_volume          double precision,
            total_put_oi        integer,
            total_call_oi       integer,
            total_put_volume    bigint,
            total_call_volume   bigint,
            computed_at         timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date)
        ) PARTITION BY RANGE (trade_date)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS pcr_daily_symbol_date
        ON features_daily.pcr_daily (symbol, trade_date DESC)
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_daily.iv_percentile_daily (
            symbol               text        NOT NULL,
            trade_date           date        NOT NULL,
            iv_current           double precision,
            iv_percentile_1y     double precision,
            iv_rank_1y           double precision,
            lookback_days        integer,
            computed_at          timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date)
        ) PARTITION BY RANGE (trade_date)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS iv_percentile_daily_symbol_date
        ON features_daily.iv_percentile_daily (symbol, trade_date DESC)
        """
    )


def _create_research_tables(cur: _Cursor) -> None:
    # --- Momentum Radar ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_signals.momentum_score_daily (
            symbol         text        NOT NULL,
            trade_date     date        NOT NULL,
            score          double precision,
            grade          text,
            path           text,
            z_sdt          double precision,
            z_v            double precision,
            accept_vwap    double precision,
            z_ofi          double precision,
            h_52w          double precision,
            o_plus         double precision,
            a_factor       double precision,
            r_sec          double precision,
            crash          double precision,
            factors_json   jsonb,
            computed_at    timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS momentum_score_daily_date_score
        ON features_signals.momentum_score_daily (trade_date DESC, score DESC)
        """
    )

    # --- GEX distribution ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_option.gex_daily (
            symbol         text        NOT NULL,
            trade_date     date        NOT NULL,
            expiry         date        NOT NULL,
            strike         double precision NOT NULL,
            call_oi        integer,
            put_oi         integer,
            call_volume    bigint,
            put_volume     bigint,
            call_gex       double precision,
            put_gex        double precision,
            net_gex        double precision,
            gex_source     text,
            computed_at    timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date, expiry, strike)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS gex_daily_symbol_date
        ON features_option.gex_daily (symbol, trade_date DESC)
        """
    )

    # --- GEX levels ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_option.gex_levels_daily (
            symbol            text        NOT NULL,
            trade_date        date        NOT NULL,
            expiry            date        NOT NULL,
            spot              double precision,
            total_net_gex     double precision,
            zero_gamma        double precision,
            major_call_wall   double precision,
            major_put_wall    double precision,
            call_wall_gex     double precision,
            put_wall_gex      double precision,
            computed_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date, expiry)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS gex_levels_daily_symbol_date
        ON features_option.gex_levels_daily (symbol, trade_date DESC)
        """
    )

    # --- IV surface ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_option.iv_surface_daily (
            symbol           text        NOT NULL,
            trade_date       date        NOT NULL,
            expiry           date        NOT NULL,
            spot             double precision,
            fit_model        text,
            smile_params     jsonb,
            surface_points   jsonb,
            vol_cone         jsonb,
            rmse             double precision,
            n_points         integer,
            computed_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date, expiry)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS iv_surface_daily_symbol_date
        ON features_option.iv_surface_daily (symbol, trade_date DESC)
        """
    )

    # --- Order sentiment ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_option.order_sentiment_daily (
            symbol                  text        NOT NULL,
            trade_date              date        NOT NULL,
            call_notional           double precision,
            put_notional            double precision,
            sentiment_score         double precision,
            call_volume             bigint,
            put_volume              bigint,
            call_oi                 integer,
            put_oi                  integer,
            pcr_volume              double precision,
            pcr_oi                  double precision,
            expiry_concentration    double precision,
            strike_concentration    double precision,
            data_source             text,
            notes                   text,
            computed_at             timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS order_sentiment_daily_date
        ON features_option.order_sentiment_daily (trade_date DESC, sentiment_score DESC)
        """
    )

    # --- Multi-leg scaffolding ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_option.multi_leg_trades (
            symbol           text        NOT NULL,
            trade_date       date        NOT NULL,
            cluster_id       text        NOT NULL,
            strategy_guess   text,
            legs             jsonb,
            total_notional   double precision,
            confidence       double precision,
            data_source      text,
            notes            text,
            computed_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date, cluster_id)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS multi_leg_trades_symbol_date
        ON features_option.multi_leg_trades (symbol, trade_date DESC)
        """
    )

    # --- Wave 4.1 Market Terrain ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_forecasts.market_terrain_daily (
            symbol            text        NOT NULL,
            trade_date        date        NOT NULL,
            pin_score         double precision,
            trend_release     double precision,
            vol_squeeze       double precision,
            tail_risk         double precision,
            expected_close    double precision,
            gamma_zone_low    double precision,
            gamma_zone_high   double precision,
            regime            text,
            spot              double precision,
            inputs_json       jsonb,
            computed_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS market_terrain_daily_date_regime
        ON features_forecasts.market_terrain_daily (trade_date DESC, regime)
        """
    )

    # --- Wave 4.2 Forecast session ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_forecasts.forecast_session (
            session_id        text        NOT NULL,
            symbol            text        NOT NULL,
            trade_date        date        NOT NULL,
            regime            text,
            spot              double precision,
            prob_rangy        double precision,
            prob_bull         double precision,
            prob_bear         double precision,
            prob_squeeze      double precision,
            expected_close    double precision,
            structures_json   jsonb,
            narrative         text,
            llm_provider      text,
            terrain_json      jsonb,
            advisory          text,
            computed_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (session_id)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS forecast_session_symbol_date
        ON features_forecasts.forecast_session (symbol, trade_date DESC)
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_forecasts.forecast_hourly (
            session_id        text        NOT NULL,
            symbol            text        NOT NULL,
            trade_date        date        NOT NULL,
            hour_et           integer     NOT NULL,
            path_call         text,
            level_low         double precision,
            level_high        double precision,
            level_target      double precision,
            confidence        double precision,
            notes             text,
            computed_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (session_id, hour_et)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS forecast_hourly_symbol_date
        ON features_forecasts.forecast_hourly (symbol, trade_date DESC)
        """
    )

    # --- Wave 4.3 Event Radar ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_signals.event_radar (
            event_id              text        NOT NULL,
            batch_id              text,
            collected_at          date,
            source                text,
            source_position       text,
            title                 text,
            raw_text              text,
            event_type            text,
            event_date            text,
            date_basis            text,
            time_orientation      text,
            certainty_evidence    text,
            subject               text,
            event_summary         text,
            key_value             text,
            value_semantics       text,
            affected_symbols      text,
            direction             integer,
            time_code             integer,
            certainty             integer,
            sentiment             integer,
            theme                 text,
            importance            integer,
            pipeline_stage        text,
            dropped               boolean     DEFAULT false,
            drop_reason           text,
            self_check_json       jsonb,
            computed_at           timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (event_id)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS event_radar_batch_collected
        ON features_signals.event_radar (batch_id, collected_at DESC)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS event_radar_importance
        ON features_signals.event_radar (collected_at DESC, importance DESC)
        """
    )

    # --- Wave 4.4 Backtest / Settlement ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_backtests.forecast_settlement (
            settlement_id     text        NOT NULL,
            session_id        text        NOT NULL,
            symbol            text        NOT NULL,
            trade_date        date        NOT NULL,
            expected_close    double precision,
            actual_close      double precision,
            close_miss        double precision,
            close_miss_pct    double precision,
            path_hit          boolean,
            path_hit_count    integer,
            path_total        integer,
            hourly_json       jsonb,
            notes             text,
            computed_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (settlement_id)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS forecast_settlement_session
        ON features_backtests.forecast_settlement (session_id)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS forecast_settlement_symbol_date
        ON features_backtests.forecast_settlement (symbol, trade_date DESC)
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_backtests.backtest_results (
            result_id               text        NOT NULL,
            symbol                  text        NOT NULL,
            period_start            date,
            period_end              date,
            sessions_settled        integer,
            path_hit_rate           double precision,
            avg_close_miss_pct      double precision,
            median_close_miss_pct   double precision,
            stats_json              jsonb,
            computed_at             timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (result_id)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS backtest_results_symbol_period
        ON features_backtests.backtest_results (symbol, period_end DESC)
        """
    )

    # --- Wave 6: Intraday Terrain ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_forecasts.terrain_intraday (
            symbol          text        NOT NULL,
            trade_date      date        NOT NULL,
            asof_ts         timestamptz NOT NULL,
            pin_score       double precision,
            trend_release   double precision,
            vol_squeeze     double precision,
            tail_risk       double precision,
            expected_close  double precision,
            gamma_zone_low  double precision,
            gamma_zone_high double precision,
            regime          text,
            spot            double precision,
            prob_rangy      double precision,
            prob_bull       double precision,
            prob_bear       double precision,
            prob_squeeze    double precision,
            inputs_json     jsonb,
            computed_at     timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date, asof_ts)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS terrain_intraday_symbol_date
        ON features_forecasts.terrain_intraday (symbol, trade_date DESC)
        """
    )

    # --- Wave 6: Intraday GEX ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_option.gex_intraday (
            symbol            text        NOT NULL,
            trade_date        date        NOT NULL,
            asof_ts           timestamptz NOT NULL,
            spot              double precision,
            total_net_gex     double precision,
            zero_gamma        double precision,
            major_call_wall   double precision,
            major_put_wall    double precision,
            levels_json       jsonb,
            computed_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date, asof_ts)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS gex_intraday_symbol_date
        ON features_option.gex_intraday (symbol, trade_date DESC)
        """
    )


    # --- Wave B: SEPA fusion (Fund + Trend Template + Momentum + Options Structure) ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features_signals.sepa_score_daily (
            symbol                 text        NOT NULL,
            trade_date             date        NOT NULL,
            -- Sub-scores 0-100
            fundamental_score      double precision,
            trend_template_score   double precision,
            momentum_score         double precision,
            structure_score        double precision,
            -- Composite 0-100
            sepa_score             double precision,
            grade                  text,
            stage                  text,
            path                   text,
            -- Flags
            trend_template_pass    boolean,
            fundamental_pass       boolean,
            -- Reference snapshot
            latest_close           double precision,
            sma_50                 double precision,
            sma_150                double precision,
            sma_200                double precision,
            high_52w               double precision,
            low_52w                double precision,
            iv_percentile          double precision,
            pcr_oi                 double precision,
            -- Sub-source counts (audit)
            fund_pass_count        integer,
            tech_pass_count        integer,
            factors_json           jsonb,
            computed_at            timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS sepa_score_daily_date_score
        ON features_signals.sepa_score_daily (trade_date DESC, sepa_score DESC)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS sepa_score_daily_stage_date
        ON features_signals.sepa_score_daily (stage, trade_date DESC)
        """
    )


def ensure_month_partitions(
    conn: _Connection,
    *,
    months_back: int = 12,
    months_forward: int = 4,
) -> None:
    """Best-effort partition extend via data_ops helper when present."""
    with conn.cursor() as cur:
        for table in MARKET_ANALYTICS_TABLES:
            try:
                cur.execute(
                    "SELECT ops_jobs.ensure_month_partitions(%s, %s, %s, %s)",
                    ("features_daily", table, months_back, months_forward),
                )
            except Exception:
                conn.rollback()
                return
    conn.commit()
