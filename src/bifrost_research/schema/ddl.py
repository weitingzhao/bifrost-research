"""Idempotent DDL for features.* Feature Store tables (Research-owned).

Wave 6.6: legacy ``features_*`` view schemas removed; canonical ``features`` only.
"""

from __future__ import annotations

from typing import Any, Protocol

from bifrost_research.schema.schemas import (
    OPTION_METRIC_PARTITIONED_TABLES,
    SCHEMA_FEATURES,
    SCHEMA_RESEARCH,
)

class _Cursor(Protocol):
    def execute(self, query: str, params: Any = None) -> Any: ...


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


# Legacy Makefile / import aliases
MARKET_ANALYTICS_TABLES = OPTION_METRIC_PARTITIONED_TABLES

RESEARCH_TABLES = (
    "stock_signal_momentum_daily",
    "option_metric_gex_daily",
    "option_metric_gex_levels_daily",
    "option_surface_iv_daily",
    "option_flow_sentiment_daily",
    "option_flow_multi_leg_daily",
    "stock_forecast_terrain_daily",
    "stock_forecast_session",
    "stock_forecast_hourly",
    "event_signal_radar_daily",
    "stock_backtest_settlement",
    "stock_backtest_results_period",
    "stock_forecast_terrain_intraday",
    "option_metric_gex_intraday",
    "stock_signal_sepa_daily",
    "stock_signal_vrp_daily",
    "stock_signal_canonical_pnl_daily",
    "option_iv_reconstructed_daily",
    "option_surface_fit_daily",
    "option_surface_residual_daily",
    "option_metric_vanna_charm_daily",
    "stock_signal_playbook_trigger_intraday",
    "stock_signal_scan_daily",
    "stock_signal_lens_hit_daily",
    "stock_signal_alert_daily",
)

# Retired bare + prefixed legacy view schema names — cleaned on db-init.
LEGACY_BARE_FEATURE_SCHEMAS = ("signals", "forecasts", "backtests")
LEGACY_FEATURE_VIEW_SCHEMAS = (
    "features_daily",
    "features_option",
    "features_signals",
    "features_forecasts",
    "features_backtests",
)


def _drop_legacy_bare_schemas(cur: _Cursor) -> None:
    for schema in LEGACY_BARE_FEATURE_SCHEMAS:
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,))
        if cur.fetchone():
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def _grant_features_schema_privileges(cur: _Cursor) -> None:
    """Best-effort GRANT on features schema (roles may not exist in dev)."""
    grants = [
        f"GRANT USAGE ON SCHEMA {SCHEMA_FEATURES} TO bifrost, analytics_writer",
        f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA {SCHEMA_FEATURES} TO bifrost, analytics_writer",
        f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA_FEATURES}
          GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO bifrost, analytics_writer
        """,
    ]
    for sql in grants:
        try:
            cur.execute(sql)
        except Exception:
            pass


def apply_features_ddl(conn: _Connection) -> None:
    """Create features schema + all Feature Store tables (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_FEATURES}")
        _grant_features_schema_privileges(cur)
        _create_option_metric_partitioned_tables(cur)
        _create_research_tables(cur)
        _drop_legacy_bare_schemas(cur)
    conn.commit()


def apply_research_workflow_ddl(conn: _Connection) -> None:
    """Wave RS-A — create research schema + hypothesis table (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_RESEARCH}")
        _create_research_workflow_tables(cur)
        _grant_research_schema_privileges(cur)
    conn.commit()


def _grant_research_schema_privileges(cur: _Cursor) -> None:
    """Best-effort GRANT on research schema (roles may not exist in dev)."""
    grants = [
        f"GRANT USAGE ON SCHEMA {SCHEMA_RESEARCH} TO bifrost, analytics_writer",
        f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA {SCHEMA_RESEARCH} TO bifrost, analytics_writer",
        f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA_RESEARCH}
          GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO bifrost, analytics_writer
        """,
    ]
    for sql in grants:
        try:
            cur.execute(sql)
        except Exception:
            pass


def _create_research_workflow_tables(cur: _Cursor) -> None:
    """Wave RS-A · research.hypothesis — first-class workflow object.

    D-RS-a locked: table lives in Golden Source ``research`` schema (OLAP domain).
    Wave RS-C4 extends the schema with ``research.backtest_run`` for
    event-driven backtest persistence.
    """
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.hypothesis (
            id                        text        PRIMARY KEY,
            title                     text        NOT NULL,
            thesis                    text        NOT NULL,
            symbols                   text[]      NOT NULL DEFAULT '{{}}'::text[],
            tags                      text[]      NOT NULL DEFAULT '{{}}'::text[],
            status                    text        NOT NULL DEFAULT 'active',
            origin_page               text,
            origin_ref                jsonb,
            linked_opportunity_ids    text[]      NOT NULL DEFAULT '{{}}'::text[],
            linked_backtest_ids       text[]      NOT NULL DEFAULT '{{}}'::text[],
            conclusion                text,
            created_at                timestamptz NOT NULL DEFAULT now(),
            updated_at                timestamptz NOT NULL DEFAULT now(),
            retired_at                timestamptz
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS hypothesis_status
        ON {SCHEMA_RESEARCH}.hypothesis (status)
        WHERE retired_at IS NULL
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS hypothesis_symbols
        ON {SCHEMA_RESEARCH}.hypothesis USING GIN (symbols)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS hypothesis_updated
        ON {SCHEMA_RESEARCH}.hypothesis (updated_at DESC)
        """
    )
    # --- Wave RS-C4: research.backtest_run ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.backtest_run (
            id                 text        PRIMARY KEY,
            hypothesis_id      text        REFERENCES {SCHEMA_RESEARCH}.hypothesis(id)
                                            ON DELETE SET NULL,
            event_def          jsonb       NOT NULL,
            strategy_template  text        NOT NULL,
            fill_config        jsonb       NOT NULL,
            lookback_years     integer     NOT NULL,
            summary            jsonb       NOT NULL,
            walk_forward       jsonb,
            benchmark          jsonb,
            created_at         timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS backtest_run_hypothesis
        ON {SCHEMA_RESEARCH}.backtest_run (hypothesis_id)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS backtest_run_created
        ON {SCHEMA_RESEARCH}.backtest_run (created_at DESC)
        """
    )
    # --- Wave RS-E3: research.ai_action_log + research.ai_draft ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.ai_action_log (
            id              text PRIMARY KEY,
            session_id      text,
            action_kind     text NOT NULL,
            action_source   text NOT NULL,
            model           text,
            input           jsonb,
            output          jsonb,
            tool_calls      jsonb,
            status          text NOT NULL DEFAULT 'proposed',
            approved_by     text,
            approved_at     timestamptz,
            executed_at     timestamptz,
            executed_result jsonb,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ai_action_log_status
        ON {SCHEMA_RESEARCH}.ai_action_log (status)
        WHERE status = 'proposed'
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ai_action_log_source
        ON {SCHEMA_RESEARCH}.ai_action_log (action_source, created_at DESC)
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.ai_draft (
            id               text PRIMARY KEY,
            kind             text NOT NULL,
            payload          jsonb NOT NULL,
            scope            text NOT NULL,
            status           text NOT NULL DEFAULT 'pending',
            generated_by     text NOT NULL,
            linked_action_id text REFERENCES {SCHEMA_RESEARCH}.ai_action_log(id)
                             ON DELETE SET NULL,
            created_at       timestamptz NOT NULL DEFAULT now(),
            expires_at       timestamptz
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ai_draft_pending
        ON {SCHEMA_RESEARCH}.ai_draft (status, created_at DESC)
        WHERE status = 'pending'
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ai_draft_scope
        ON {SCHEMA_RESEARCH}.ai_draft (scope)
        """
    )
    # --- Wave RS-F4: research.copilot_session ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.copilot_session (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id      text NOT NULL DEFAULT 'owner',
            title         text,
            model         text NOT NULL,
            agent_trail   jsonb DEFAULT '[]'::jsonb,
            messages      jsonb NOT NULL DEFAULT '[]'::jsonb,
            hypothesis_id text REFERENCES {SCHEMA_RESEARCH}.hypothesis(id) ON DELETE SET NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),
            expires_at    timestamptz NOT NULL DEFAULT (now() + interval '1 year'),
            status        text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','archived','expired'))
        )
        """
    )
    # Wave RS-UX5: pinned flag for sorting favorite sessions at the top.
    cur.execute(
        f"""
        ALTER TABLE {SCHEMA_RESEARCH}.copilot_session
        ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false
        """
    )
    # RS-KB QA: session group label (folder-like grouping, owner-scoped)
    cur.execute(
        f"""
        ALTER TABLE {SCHEMA_RESEARCH}.copilot_session
        ADD COLUMN IF NOT EXISTS group_name text
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_copilot_session_group
        ON {SCHEMA_RESEARCH}.copilot_session (owner_id, group_name)
        WHERE group_name IS NOT NULL
        """
    )
    # RS-KB1: extend short-lived sessions to 1-year sliding retention.
    cur.execute(
        f"""
        UPDATE {SCHEMA_RESEARCH}.copilot_session
        SET expires_at = updated_at + interval '1 year'
        WHERE expires_at IS NULL
           OR expires_at < now() + interval '30 days'
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_copilot_session_owner
        ON {SCHEMA_RESEARCH}.copilot_session (owner_id, updated_at DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_copilot_session_hyp
        ON {SCHEMA_RESEARCH}.copilot_session (hypothesis_id)
        """
    )

    # --- Wave RS-EX2: Context Bridge audit log ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.copilot_bridge_event (
            id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id                text NOT NULL,
            session_id              uuid NOT NULL REFERENCES {SCHEMA_RESEARCH}.copilot_session(id) ON DELETE CASCADE,
            focus                   text NOT NULL,
            depth                   text NOT NULL,
            target                  text NOT NULL,
            model                   text NOT NULL,
            frames_from_message_id  text,
            input_tokens            int NOT NULL DEFAULT 0,
            output_tokens           int NOT NULL DEFAULT 0,
            cost_usd                numeric(12, 6) NOT NULL DEFAULT 0,
            preview_md              text NOT NULL,
            polished                boolean NOT NULL DEFAULT true,
            created_at              timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_copilot_bridge_owner_day
        ON {SCHEMA_RESEARCH}.copilot_bridge_event (owner_id, created_at DESC)
        """
    )

    # --- RS-KB3: Playbook (personal trading system DNA) ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.playbook_rule (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id            text NOT NULL,
            title               text NOT NULL,
            category            text NOT NULL DEFAULT 'general',
            body_md             text NOT NULL,
            trigger_ctx         jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            tags                text[] NOT NULL DEFAULT '{{}}'::text[],
            active              boolean NOT NULL DEFAULT true,
            source_session_id   uuid REFERENCES {SCHEMA_RESEARCH}.copilot_session(id) ON DELETE SET NULL,
            source_msg_ref      jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            retired_at          timestamptz
        )
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.playbook_case (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id            text NOT NULL,
            trade_ref           jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            outcome             text,
            lessons_md          text NOT NULL,
            tags                text[] NOT NULL DEFAULT '{{}}'::text[],
            related_rule_ids    uuid[] NOT NULL DEFAULT '{{}}'::uuid[],
            created_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.playbook_note (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id            text NOT NULL,
            note_md             text NOT NULL,
            tags                text[] NOT NULL DEFAULT '{{}}'::text[],
            symbols             text[] NOT NULL DEFAULT '{{}}'::text[],
            source_session_id   uuid REFERENCES {SCHEMA_RESEARCH}.copilot_session(id) ON DELETE SET NULL,
            created_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_playbook_rule_owner
        ON {SCHEMA_RESEARCH}.playbook_rule (owner_id, category, active)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_playbook_rule_tags
        ON {SCHEMA_RESEARCH}.playbook_rule USING gin (tags)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_playbook_note_owner
        ON {SCHEMA_RESEARCH}.playbook_note (owner_id, created_at DESC)
        """
    )

    # --- Wave RS-PS1: owner-scoped agent personas ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.agent_persona (
            owner_id            text NOT NULL,
            agent_name          text NOT NULL,
            persona_md          text NOT NULL DEFAULT '',
            preferences_json    jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            guardrail_locked    boolean NOT NULL DEFAULT false,
            seeded              boolean NOT NULL DEFAULT false,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (owner_id, agent_name)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_agent_persona_owner
        ON {SCHEMA_RESEARCH}.agent_persona (owner_id)
        """
    )

    # --- Wave RS-PS3: playbook rule → agent owner ---
    cur.execute(
        f"""
        ALTER TABLE {SCHEMA_RESEARCH}.playbook_rule
        ADD COLUMN IF NOT EXISTS agent_owner text DEFAULT 'shared'
        """
    )
    cur.execute(
        f"""
        UPDATE {SCHEMA_RESEARCH}.playbook_rule
        SET agent_owner = 'shared'
        WHERE agent_owner IS NULL
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_playbook_rule_agent_owner
        ON {SCHEMA_RESEARCH}.playbook_rule (owner_id, agent_owner, active)
        WHERE active = true
        """
    )

    # --- RS-KB5: semantic retrieval store (pgvector optional) ---
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.embedding_chunk (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                owner_id        text NOT NULL,
                entity_type     text NOT NULL,
                entity_id       uuid NOT NULL,
                chunk_id        int NOT NULL DEFAULT 0,
                content         text NOT NULL,
                embedding       vector(1024),
                created_at      timestamptz NOT NULL DEFAULT now(),
                UNIQUE (entity_type, entity_id, chunk_id)
            )
            """
        )
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_embedding_owner_type
            ON {SCHEMA_RESEARCH}.embedding_chunk (owner_id, entity_type)
            """
        )
    except Exception:
        # pgvector not available — keyword search fallback (RS-KB5)
        pass

    # --- Wave Loop v1: research.candidate_pool ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.candidate_pool (
            id            text        PRIMARY KEY,
            trade_date    date        NOT NULL,
            symbol        text        NOT NULL,
            source        text        NOT NULL,
            source_ref    jsonb,
            score         numeric,
            lens_snapshot jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
            tags          text[]      NOT NULL DEFAULT '{{}}'::text[],
            status        text        NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open','promoted','dismissed','expired')),
            hypothesis_id text        REFERENCES {SCHEMA_RESEARCH}.hypothesis(id)
                                      ON DELETE SET NULL,
            owner_id      text        NOT NULL DEFAULT 'owner',
            created_at    timestamptz NOT NULL DEFAULT now(),
            ttl_at        timestamptz
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS candidate_pool_open
        ON {SCHEMA_RESEARCH}.candidate_pool (owner_id, status, trade_date DESC)
        WHERE status = 'open'
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS candidate_pool_symbol
        ON {SCHEMA_RESEARCH}.candidate_pool (symbol, trade_date DESC)
        """
    )

    # --- Wave W2: research.candidate_outcome ---
    # One row per (candidate, horizon) so 1d / 5d / 20d coexist instead of
    # widening candidate_pool once per horizon.
    #
    # `hit` is "beat the benchmark", not "went up".  Candidates carry no
    # direction — they are "look at this symbol", not a long or a short — and an
    # absolute win rate mostly measures the market: over 2026-08-23..28 every
    # liquid symbol averaged a 40.2% 3-day win rate regardless of any signal.
    # The raw legs stay on the row, so an absolute rate is still derivable.
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.candidate_outcome (
            id               text        PRIMARY KEY,
            candidate_id     text        NOT NULL
                             REFERENCES {SCHEMA_RESEARCH}.candidate_pool(id)
                             ON DELETE CASCADE,
            symbol           text        NOT NULL,
            trade_date       date        NOT NULL,
            horizon_days     integer     NOT NULL CHECK (horizon_days > 0),
            entry_close      numeric,
            exit_close       numeric,
            exit_date        date,
            forward_return   numeric,
            benchmark_symbol text,
            benchmark_return numeric,
            excess_return    numeric,
            hit              boolean,
            settled_at       timestamptz NOT NULL DEFAULT now(),
            UNIQUE (candidate_id, horizon_days)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS candidate_outcome_horizon
        ON {SCHEMA_RESEARCH}.candidate_outcome (horizon_days, trade_date DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS candidate_outcome_symbol
        ON {SCHEMA_RESEARCH}.candidate_outcome (symbol, trade_date DESC)
        """
    )

    # --- Wave Harness: research.objective + objective_run ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.objective (
            id            text        PRIMARY KEY,
            title         text        NOT NULL,
            description   text        NOT NULL,
            schedule      text        NOT NULL,
            policy_json   jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
            persona       text        NOT NULL DEFAULT 'loop_curator',
            status        text        NOT NULL DEFAULT 'active',
            owner_id      text        NOT NULL DEFAULT 'owner',
            created_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_RESEARCH}.objective_run (
            id            text        PRIMARY KEY,
            objective_id  text        NOT NULL REFERENCES {SCHEMA_RESEARCH}.objective(id),
            started_at    timestamptz NOT NULL DEFAULT now(),
            finished_at   timestamptz,
            plan_json     jsonb,
            trace_json    jsonb,
            outputs       jsonb,
            status        text        NOT NULL DEFAULT 'running'
                          CHECK (status IN (
                            'running','awaiting_approval','completed','failed','cancelled'
                          ))
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS objective_run_objective
        ON {SCHEMA_RESEARCH}.objective_run (objective_id, started_at DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS objective_run_status
        ON {SCHEMA_RESEARCH}.objective_run (status, started_at DESC)
        """
    )

    # Wave C: copilot_session.candidate_ids for Loop attach
    cur.execute(
        f"""
        ALTER TABLE {SCHEMA_RESEARCH}.copilot_session
        ADD COLUMN IF NOT EXISTS candidate_ids text[] NOT NULL DEFAULT '{{}}'::text[]
        """
    )


def apply_features_daily_ddl(conn: _Connection) -> None:
    """Legacy wrapper — partitioned option metrics + compat views."""
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_FEATURES}")
        _create_option_metric_partitioned_tables(cur)
    conn.commit()


def apply_research_ddl(conn: _Connection) -> None:
    """Legacy wrapper — non-partitioned feature tables + compat views."""
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_FEATURES}")
        _create_research_tables(cur)
        _drop_legacy_bare_schemas(cur)
    conn.commit()


def apply_all_ddl(conn: _Connection) -> None:
    """Apply full Feature Store DDL and drop retired legacy view schemas."""
    apply_features_ddl(conn)
    apply_research_workflow_ddl(conn)
    drop_legacy_feature_schemas(conn)


# Legacy Makefile / import alias (market_analytics → features option metrics).
apply_market_analytics_ddl = apply_features_daily_ddl


def _create_option_metric_partitioned_tables(cur: _Cursor) -> None:
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_metric_max_pain_daily (
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
        f"""
        CREATE INDEX IF NOT EXISTS option_metric_max_pain_daily_symbol_date
        ON {SCHEMA_FEATURES}.option_metric_max_pain_daily (symbol, trade_date DESC)
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_metric_atm_iv_daily (
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
        f"""
        CREATE INDEX IF NOT EXISTS option_metric_atm_iv_daily_symbol_date
        ON {SCHEMA_FEATURES}.option_metric_atm_iv_daily (symbol, trade_date DESC)
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_metric_pcr_daily (
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
        f"""
        CREATE INDEX IF NOT EXISTS option_metric_pcr_daily_symbol_date
        ON {SCHEMA_FEATURES}.option_metric_pcr_daily (symbol, trade_date DESC)
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_metric_iv_percentile_daily (
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
        f"""
        CREATE INDEX IF NOT EXISTS option_metric_iv_percentile_daily_symbol_date
        ON {SCHEMA_FEATURES}.option_metric_iv_percentile_daily (symbol, trade_date DESC)
        """
    )


def _create_research_tables(cur: _Cursor) -> None:
  # --- Momentum Radar ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_signal_momentum_daily (
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
        f"""
        CREATE INDEX IF NOT EXISTS stock_signal_momentum_daily_date_score
        ON {SCHEMA_FEATURES}.stock_signal_momentum_daily (trade_date DESC, score DESC)
        """
    )

    # --- GEX distribution ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_metric_gex_daily (
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
        f"""
        CREATE INDEX IF NOT EXISTS option_metric_gex_daily_symbol_date
        ON {SCHEMA_FEATURES}.option_metric_gex_daily (symbol, trade_date DESC)
        """
    )

    # --- GEX levels ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_metric_gex_levels_daily (
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
        f"""
        CREATE INDEX IF NOT EXISTS option_metric_gex_levels_daily_symbol_date
        ON {SCHEMA_FEATURES}.option_metric_gex_levels_daily (symbol, trade_date DESC)
        """
    )

    # --- IV surface ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_surface_iv_daily (
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
        f"""
        CREATE INDEX IF NOT EXISTS option_surface_iv_daily_symbol_date
        ON {SCHEMA_FEATURES}.option_surface_iv_daily (symbol, trade_date DESC)
        """
    )

    # --- Order sentiment ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_flow_sentiment_daily (
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
        f"""
        CREATE INDEX IF NOT EXISTS option_flow_sentiment_daily_date
        ON {SCHEMA_FEATURES}.option_flow_sentiment_daily (trade_date DESC, sentiment_score DESC)
        """
    )

    # --- Multi-leg scaffolding ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_flow_multi_leg_daily (
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
        f"""
        CREATE INDEX IF NOT EXISTS option_flow_multi_leg_daily_symbol_date
        ON {SCHEMA_FEATURES}.option_flow_multi_leg_daily (symbol, trade_date DESC)
        """
    )

    # --- Market Terrain ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_forecast_terrain_daily (
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
        f"""
        CREATE INDEX IF NOT EXISTS stock_forecast_terrain_daily_date_regime
        ON {SCHEMA_FEATURES}.stock_forecast_terrain_daily (trade_date DESC, regime)
        """
    )

    # --- Forecast session ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_forecast_session (
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
        f"""
        CREATE INDEX IF NOT EXISTS stock_forecast_session_symbol_date
        ON {SCHEMA_FEATURES}.stock_forecast_session (symbol, trade_date DESC)
        """
    )

    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_forecast_hourly (
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
        f"""
        CREATE INDEX IF NOT EXISTS stock_forecast_hourly_symbol_date
        ON {SCHEMA_FEATURES}.stock_forecast_hourly (symbol, trade_date DESC)
        """
    )

    # --- Event Radar ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.event_signal_radar_daily (
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
        f"""
        CREATE INDEX IF NOT EXISTS event_signal_radar_daily_batch_collected
        ON {SCHEMA_FEATURES}.event_signal_radar_daily (batch_id, collected_at DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS event_signal_radar_daily_importance
        ON {SCHEMA_FEATURES}.event_signal_radar_daily (collected_at DESC, importance DESC)
        """
    )

    # --- Backtest / Settlement ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_backtest_settlement (
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
            stats_json        jsonb,
            PRIMARY KEY (settlement_id)
        )
        """
    )
    cur.execute(
        f"""
        ALTER TABLE {SCHEMA_FEATURES}.stock_backtest_settlement
        ADD COLUMN IF NOT EXISTS stats_json jsonb
        """
    )

    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.macro_event_daily (
            macro_id          text        NOT NULL,
            event_date        date        NOT NULL,
            release_ts        timestamptz,
            country           text,
            indicator         text        NOT NULL,
            actual_value      double precision,
            expected_value    double precision,
            prior_value       double precision,
            unit              text,
            gap_pct           double precision,
            forward_flag      boolean     NOT NULL DEFAULT false,
            source            text,
            notes             text,
            computed_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (macro_id)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS macro_event_daily_date
        ON {SCHEMA_FEATURES}.macro_event_daily (event_date DESC)
        """
    )

    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_forecast_hourly_session (
            hourly_session_id text        NOT NULL,
            parent_session_id text        NOT NULL,
            symbol            text        NOT NULL,
            trade_date        date        NOT NULL,
            hour_et           smallint    NOT NULL,
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
            terrain_json        jsonb,
            advisory          text,
            computed_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (hourly_session_id),
            UNIQUE (parent_session_id, hour_et)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS stock_backtest_settlement_session
        ON {SCHEMA_FEATURES}.stock_backtest_settlement (session_id)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS stock_backtest_settlement_symbol_date
        ON {SCHEMA_FEATURES}.stock_backtest_settlement (symbol, trade_date DESC)
        """
    )

    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_backtest_results_period (
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
        f"""
        CREATE INDEX IF NOT EXISTS stock_backtest_results_period_symbol_period
        ON {SCHEMA_FEATURES}.stock_backtest_results_period (symbol, period_end DESC)
        """
    )

    # --- Intraday Terrain ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_forecast_terrain_intraday (
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
        f"""
        CREATE INDEX IF NOT EXISTS stock_forecast_terrain_intraday_symbol_date
        ON {SCHEMA_FEATURES}.stock_forecast_terrain_intraday (symbol, trade_date DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ix_terrain_intraday_pit
        ON {SCHEMA_FEATURES}.stock_forecast_terrain_intraday (symbol, trade_date, asof_ts DESC)
        """
    )

    # --- Intraday GEX ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_metric_gex_intraday (
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
        f"""
        CREATE INDEX IF NOT EXISTS option_metric_gex_intraday_symbol_date
        ON {SCHEMA_FEATURES}.option_metric_gex_intraday (symbol, trade_date DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ix_gex_intraday_pit
        ON {SCHEMA_FEATURES}.option_metric_gex_intraday (symbol, trade_date, asof_ts DESC)
        """
    )

    # --- SEPA projection target (dbt owns logic; Python projection writes here) ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_signal_sepa_daily (
            symbol                 text        NOT NULL,
            trade_date             date        NOT NULL,
            fundamental_score      double precision,
            trend_template_score   double precision,
            momentum_score         double precision,
            structure_score        double precision,
            sepa_score             double precision,
            grade                  text,
            stage                  text,
            path                   text,
            trend_template_pass    boolean,
            fundamental_pass       boolean,
            latest_close           double precision,
            sma_50                 double precision,
            sma_150                double precision,
            sma_200                double precision,
            high_52w               double precision,
            low_52w                double precision,
            iv_percentile          double precision,
            pcr_oi                 double precision,
            fund_pass_count        integer,
            tech_pass_count        integer,
            factors_json           jsonb,
            asof_ts                timestamptz,
            computed_at            timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS stock_signal_sepa_daily_date_score
        ON {SCHEMA_FEATURES}.stock_signal_sepa_daily (trade_date DESC, sepa_score DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS stock_signal_sepa_daily_stage_date
        ON {SCHEMA_FEATURES}.stock_signal_sepa_daily (stage, trade_date DESC)
        """
    )
    # Idempotent column add for existing deployments
    cur.execute(
        f"""
        ALTER TABLE {SCHEMA_FEATURES}.stock_signal_sepa_daily
        ADD COLUMN IF NOT EXISTS asof_ts timestamptz
        """
    )
    cur.execute(
        f"""
        COMMENT ON COLUMN {SCHEMA_FEATURES}.stock_signal_sepa_daily.asof_ts IS
        'Last projection timestamp (Wave 12). Daily UPSERT overwrite — NOT a historical PIT snapshot.'
        """
    )

    # --- Wave RS-B-VRP1: IV-RV Spread (Volatility Risk Premium) ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_signal_vrp_daily (
            symbol        text        NOT NULL,
            trade_date    date        NOT NULL,
            rv_20d        double precision,
            rv_60d        double precision,
            rv_252d       double precision,
            atm_iv_30d    double precision,
            vrp_20d       double precision,
            vrp_60d       double precision,
            vrp_pct_252d  double precision,
            fwd_ret_20d   double precision,
            computed_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS stock_signal_vrp_daily_pct
        ON {SCHEMA_FEATURES}.stock_signal_vrp_daily (vrp_pct_252d)
        WHERE vrp_pct_252d IS NOT NULL
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS stock_signal_vrp_daily_date_symbol
        ON {SCHEMA_FEATURES}.stock_signal_vrp_daily (trade_date DESC, symbol)
        """
    )

    # --- Wave Canonical-PnL Foundation: dual-write features + dw_stock mart ---
    cur.execute("CREATE SCHEMA IF NOT EXISTS dw_stock")
    _canonical_pnl_ddl = """
        (
            as_of_date       date         NOT NULL,
            entry_date       date         NOT NULL,
            symbol           text         NOT NULL,
            structure        text         NOT NULL,
            params_hash      text         NOT NULL,
            structure_params jsonb        NOT NULL DEFAULT '{}'::jsonb,
            entry_spot       double precision,
            entry_atm_iv     double precision,
            entry_mid        double precision,
            as_of_spot       double precision,
            as_of_atm_iv     double precision,
            mtm_value        double precision,
            pnl_since_entry  double precision,
            dte_remaining    integer,
            expired          boolean      NOT NULL DEFAULT false,
            final_pnl        double precision,
            data_quality     text         NOT NULL DEFAULT 'ok',
            computed_at      timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (as_of_date, entry_date, symbol, structure, params_hash)
        )
    """
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_signal_canonical_pnl_daily {_canonical_pnl_ddl}"
    )
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS dw_stock.mart_canonical_pnl_daily {_canonical_pnl_ddl}"
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS stock_signal_canonical_pnl_symbol_entry
        ON {SCHEMA_FEATURES}.stock_signal_canonical_pnl_daily (symbol, entry_date, structure)
        """
    )

    # --- IDS Historical IV Solver: dual-source reconstructed IV ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_iv_reconstructed_daily (
            symbol          text        NOT NULL,
            option_ticker   text        NOT NULL,
            trade_date      date        NOT NULL,
            strike          double precision,
            expiry          date,
            option_right    text,
            mid_price       double precision,
            spot            double precision,
            tte_years       double precision,
            iv              double precision,
            delta           double precision,
            gamma           double precision,
            solver_status   text        NOT NULL DEFAULT 'ok',
            computed_at     timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, option_ticker, trade_date)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS option_iv_reconstructed_symbol_date
        ON {SCHEMA_FEATURES}.option_iv_reconstructed_daily (symbol, trade_date)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS option_iv_reconstructed_trade_date
        ON {SCHEMA_FEATURES}.option_iv_reconstructed_daily (trade_date DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS option_iv_reconstructed_ok_iv
        ON {SCHEMA_FEATURES}.option_iv_reconstructed_daily (trade_date DESC, symbol)
        WHERE iv IS NOT NULL
        """
    )
    # Unified ATM preference view: reconstructed first, then live snapshot (IDS-4)
    cur.execute(
        f"""
        CREATE OR REPLACE VIEW {SCHEMA_FEATURES}.v_atm_iv_unified AS
        SELECT
          r.option_ticker,
          r.symbol AS underlying,
          r.iv,
          r.spot AS underlying_price,
          r.expiry,
          r.strike,
          r.option_right,
          r.trade_date,
          r.solver_status AS iv_source
        FROM {SCHEMA_FEATURES}.option_iv_reconstructed_daily r
        WHERE r.iv IS NOT NULL AND r.iv > 0
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS mart_canonical_pnl_symbol_entry
        ON dw_stock.mart_canonical_pnl_daily (symbol, entry_date, structure)
        """
    )

    # --- Wave RS-B-Surface1: SVI Vol Surface fit + per-strike residuals ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_surface_fit_daily (
            symbol        text        NOT NULL,
            trade_date    date        NOT NULL,
            expiry        date        NOT NULL,
            dte           integer     NOT NULL,
            svi_a         double precision,
            svi_b         double precision,
            svi_rho       double precision,
            svi_m         double precision,
            svi_sigma     double precision,
            atm_vol       double precision,
            atm_slope     double precision,
            fit_rmse      double precision,
            n_points      integer,
            computed_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date, expiry)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS option_surface_fit_daily_symbol_date
        ON {SCHEMA_FEATURES}.option_surface_fit_daily (symbol, trade_date DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS option_surface_fit_daily_date_dte
        ON {SCHEMA_FEATURES}.option_surface_fit_daily (trade_date DESC, dte)
        """
    )

    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_surface_residual_daily (
            symbol        text        NOT NULL,
            trade_date    date        NOT NULL,
            expiry        date        NOT NULL,
            strike        double precision NOT NULL,
            log_moneyness double precision,
            iv_market     double precision,
            iv_fitted     double precision,
            residual      double precision,
            residual_z    double precision,
            computed_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date, expiry, strike)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS option_surface_residual_z_abs
        ON {SCHEMA_FEATURES}.option_surface_residual_daily (abs(residual_z) DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS option_surface_residual_symbol_date
        ON {SCHEMA_FEATURES}.option_surface_residual_daily (symbol, trade_date DESC, expiry)
        """
    )

    # --- Wave RS-B-OpEx1: Vanna/Charm + OpEx cycle daily ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.option_metric_vanna_charm_daily (
            symbol             text        NOT NULL,
            trade_date         date        NOT NULL,
            spot               double precision,
            total_vanna        double precision,
            total_charm        double precision,
            vanna_zero_strike  double precision,
            charm_zero_strike  double precision,
            dte_to_opex        integer,
            is_opex_week       boolean,
            computed_at        timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS option_metric_vanna_charm_symbol_date
        ON {SCHEMA_FEATURES}.option_metric_vanna_charm_daily (symbol, trade_date DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS option_metric_vanna_charm_opex_week
        ON {SCHEMA_FEATURES}.option_metric_vanna_charm_daily (trade_date DESC)
        WHERE is_opex_week = true
        """
    )

    # --- Analyze C.2: Playbook scenario trigger event-log ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_signal_playbook_trigger_intraday (
            symbol              text        NOT NULL,
            trade_date          date        NOT NULL,
            scenario_key        text        NOT NULL,
            trigger_at          timestamptz NOT NULL,
            satisfied           boolean     NOT NULL DEFAULT false,
            condition_snapshot  jsonb,
            computed_at         timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, trade_date, scenario_key, trigger_at)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS stock_signal_playbook_trigger_symbol_date
        ON {SCHEMA_FEATURES}.stock_signal_playbook_trigger_intraday
            (symbol, trade_date DESC, trigger_at DESC)
        """
    )

    # --- Analyze Wave D: materialized multi-lens scanner ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_signal_scan_daily (
            trade_date          date        NOT NULL,
            symbol              text        NOT NULL,
            close               double precision,
            iv_rank_1y          double precision,
            vrp_pct_252d        double precision,
            atm_slope_30d       double precision,
            pin_pct_distance    double precision,
            dte_to_opex         integer,
            zero_gamma_offset   double precision,
            gex_notional        double precision,
            terrain_regime      text,
            pin_score           double precision,
            tail_risk           double precision,
            trend_release       double precision,
            composite_score     double precision,
            lens_flags          jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            computed_at         timestamptz NOT NULL,
            fetched_at          timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (trade_date, symbol)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS stock_signal_scan_daily_date_score
        ON {SCHEMA_FEATURES}.stock_signal_scan_daily (trade_date DESC, composite_score DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS stock_signal_scan_daily_symbol_date
        ON {SCHEMA_FEATURES}.stock_signal_scan_daily (symbol, trade_date DESC)
        """
    )



    # --- Analyze Wave I: lens trigger hit / signal decay ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_signal_lens_hit_daily (
            trade_date        date        NOT NULL,
            symbol            text        NOT NULL,
            lens              text        NOT NULL,
            trigger_side      text        NOT NULL,
            trigger_value     double precision,
            fwd_return_5d     double precision,
            fwd_return_20d    double precision,
            hit_5d            boolean,
            hit_20d           boolean,
            computed_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (trade_date, symbol, lens, trigger_side)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS stock_signal_lens_hit_daily_symbol_lens_date
        ON {SCHEMA_FEATURES}.stock_signal_lens_hit_daily (symbol, lens, trade_date DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS stock_signal_lens_hit_daily_lens_date
        ON {SCHEMA_FEATURES}.stock_signal_lens_hit_daily (lens, trade_date DESC)
        """
    )

    # --- Analyze Wave M: daily analyze alerts ---
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_FEATURES}.stock_signal_alert_daily (
            trade_date        date        NOT NULL,
            kind              text        NOT NULL,
            symbol            text        NOT NULL DEFAULT '',
            lens              text        NOT NULL DEFAULT '',
            severity          text        NOT NULL DEFAULT 'info',
            reason_json       jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
            computed_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (trade_date, kind, symbol, lens)
        )
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS stock_signal_alert_daily_date_sev
        ON {SCHEMA_FEATURES}.stock_signal_alert_daily (trade_date DESC, severity)
        """
    )



def ensure_month_partitions(
    conn: _Connection,
    *,
    months_back: int = 12,
    months_forward: int = 4,
) -> None:
    """Best-effort partition extend via ops_jobs helper when present."""
    with conn.cursor() as cur:
        for table in OPTION_METRIC_PARTITIONED_TABLES:
            try:
                cur.execute(
                    "SELECT ops_jobs.ensure_month_partitions(%s, %s, %s, %s)",
                    (SCHEMA_FEATURES, table, months_back, months_forward),
                )
            except Exception:
                conn.rollback()
                return
    conn.commit()


def drop_legacy_feature_schemas(conn: _Connection) -> None:
    """Wave 6.6 — drop legacy features_* view schemas (best-effort per schema)."""
    with conn.cursor() as cur:
        for legacy_schema in LEGACY_FEATURE_VIEW_SCHEMAS:
            try:
                cur.execute(f"DROP SCHEMA IF EXISTS {legacy_schema} CASCADE")
                conn.commit()
            except Exception:
                conn.rollback()
