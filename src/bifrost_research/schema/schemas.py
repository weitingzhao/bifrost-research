"""Canonical Golden Source schema names for Research-owned feature tables.

Wave 6.4–6.6: single ``features`` schema with four-part table names
``{asset}_{stage}_{name}_{granularity}``.
"""

SCHEMA_FEATURES = "features"

# Wave RS-A — Hypothesis + Research workflow objects (Golden Source, D-RS-a).
SCHEMA_RESEARCH = "research"
TABLE_RESEARCH_HYPOTHESIS = f"{SCHEMA_RESEARCH}.hypothesis"

# Wave RS-C4 — event-driven backtest runs (colocated with hypotheses).
TABLE_RESEARCH_BACKTEST_RUN = f"{SCHEMA_RESEARCH}.backtest_run"

# Wave RS-E3 — AI draft inbox + action audit log (D-RS-E-e/g).
TABLE_RESEARCH_AI_ACTION_LOG = f"{SCHEMA_RESEARCH}.ai_action_log"
TABLE_RESEARCH_AI_DRAFT = f"{SCHEMA_RESEARCH}.ai_draft"
TABLE_RESEARCH_COPILOT_SESSION = f"{SCHEMA_RESEARCH}.copilot_session"
TABLE_RESEARCH_COPILOT_BRIDGE_EVENT = f"{SCHEMA_RESEARCH}.copilot_bridge_event"
TABLE_RESEARCH_PLAYBOOK_RULE = f"{SCHEMA_RESEARCH}.playbook_rule"
TABLE_RESEARCH_PLAYBOOK_CASE = f"{SCHEMA_RESEARCH}.playbook_case"
TABLE_RESEARCH_PLAYBOOK_NOTE = f"{SCHEMA_RESEARCH}.playbook_note"
TABLE_RESEARCH_EMBEDDING_CHUNK = f"{SCHEMA_RESEARCH}.embedding_chunk"
TABLE_RESEARCH_AGENT_PERSONA = f"{SCHEMA_RESEARCH}.agent_persona"

# Wave Loop v1 — Candidate Pool (Discover → Analyze bridge).
TABLE_RESEARCH_CANDIDATE_POOL = f"{SCHEMA_RESEARCH}.candidate_pool"

# Wave W2 — what happened to a candidate after it was proposed.  Without this
# the Loop can propose forever and never learn: "is this any good?" has no
# answer anywhere in the system.
TABLE_RESEARCH_CANDIDATE_OUTCOME = f"{SCHEMA_RESEARCH}.candidate_outcome"

# Wave Harness — Objective + run audit (Stage 3).
TABLE_RESEARCH_OBJECTIVE = f"{SCHEMA_RESEARCH}.objective"
TABLE_RESEARCH_OBJECTIVE_RUN = f"{SCHEMA_RESEARCH}.objective_run"

# --- Canonical table names (schema.table) ---
TABLE_OPTION_METRIC_ATM_IV_DAILY = f"{SCHEMA_FEATURES}.option_metric_atm_iv_daily"
TABLE_OPTION_METRIC_MAX_PAIN_DAILY = f"{SCHEMA_FEATURES}.option_metric_max_pain_daily"
TABLE_OPTION_METRIC_PCR_DAILY = f"{SCHEMA_FEATURES}.option_metric_pcr_daily"
TABLE_OPTION_METRIC_IV_PERCENTILE_DAILY = f"{SCHEMA_FEATURES}.option_metric_iv_percentile_daily"
TABLE_OPTION_METRIC_GEX_DAILY = f"{SCHEMA_FEATURES}.option_metric_gex_daily"
TABLE_OPTION_METRIC_GEX_INTRADAY = f"{SCHEMA_FEATURES}.option_metric_gex_intraday"
TABLE_OPTION_METRIC_GEX_LEVELS_DAILY = f"{SCHEMA_FEATURES}.option_metric_gex_levels_daily"
TABLE_OPTION_SURFACE_IV_DAILY = f"{SCHEMA_FEATURES}.option_surface_iv_daily"
TABLE_OPTION_FLOW_SENTIMENT_DAILY = f"{SCHEMA_FEATURES}.option_flow_sentiment_daily"
TABLE_OPTION_FLOW_MULTI_LEG_DAILY = f"{SCHEMA_FEATURES}.option_flow_multi_leg_daily"
# IDS Historical IV Solver — dual-source reconstructed IV (OHLCV Brent + vendor snapshot)
TABLE_OPTION_IV_RECONSTRUCTED_DAILY = f"{SCHEMA_FEATURES}.option_iv_reconstructed_daily"
TABLE_STOCK_SIGNAL_MOMENTUM_DAILY = f"{SCHEMA_FEATURES}.stock_signal_momentum_daily"
TABLE_STOCK_SIGNAL_SEPA_DAILY = f"{SCHEMA_FEATURES}.stock_signal_sepa_daily"
TABLE_STOCK_SIGNAL_VRP_DAILY = f"{SCHEMA_FEATURES}.stock_signal_vrp_daily"
# Wave Canonical-PnL Foundation — dual layer (features projection + dw_stock mart)
TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY = f"{SCHEMA_FEATURES}.stock_signal_canonical_pnl_daily"
SCHEMA_DW_STOCK = "dw_stock"
TABLE_MART_CANONICAL_PNL_DAILY = f"{SCHEMA_DW_STOCK}.mart_canonical_pnl_daily"
TABLE_EVENT_SIGNAL_RADAR_DAILY = f"{SCHEMA_FEATURES}.event_signal_radar_daily"
TABLE_STOCK_FORECAST_TERRAIN_DAILY = f"{SCHEMA_FEATURES}.stock_forecast_terrain_daily"
TABLE_STOCK_FORECAST_TERRAIN_INTRADAY = f"{SCHEMA_FEATURES}.stock_forecast_terrain_intraday"
TABLE_STOCK_FORECAST_SESSION = f"{SCHEMA_FEATURES}.stock_forecast_session"
TABLE_STOCK_FORECAST_HOURLY = f"{SCHEMA_FEATURES}.stock_forecast_hourly"
TABLE_STOCK_BACKTEST_SETTLEMENT = f"{SCHEMA_FEATURES}.stock_backtest_settlement"
TABLE_STOCK_BACKTEST_RESULTS_PERIOD = f"{SCHEMA_FEATURES}.stock_backtest_results_period"
# Analyze C.2 — playbook scenario trigger event-log
TABLE_STOCK_SIGNAL_PLAYBOOK_TRIGGER_INTRADAY = (
    f"{SCHEMA_FEATURES}.stock_signal_playbook_trigger_intraday"
)
# Analyze Wave D — materialized multi-lens scanner
TABLE_STOCK_SIGNAL_SCAN_DAILY = f"{SCHEMA_FEATURES}.stock_signal_scan_daily"
# Analyze Wave I — lens trigger hit / signal decay
TABLE_STOCK_SIGNAL_LENS_HIT_DAILY = f"{SCHEMA_FEATURES}.stock_signal_lens_hit_daily"
# Analyze Wave M — daily analyze alerts
TABLE_STOCK_SIGNAL_ALERT_DAILY = f"{SCHEMA_FEATURES}.stock_signal_alert_daily"

# Partitioned option metric daily tables (for ensure_month_partitions)
OPTION_METRIC_PARTITIONED_TABLES = (
    "option_metric_max_pain_daily",
    "option_metric_atm_iv_daily",
    "option_metric_pcr_daily",
    "option_metric_iv_percentile_daily",
)

CANONICAL_FEATURE_TABLES = (
    TABLE_OPTION_METRIC_ATM_IV_DAILY,
    TABLE_OPTION_METRIC_MAX_PAIN_DAILY,
    TABLE_OPTION_METRIC_PCR_DAILY,
    TABLE_OPTION_METRIC_IV_PERCENTILE_DAILY,
    TABLE_OPTION_METRIC_GEX_DAILY,
    TABLE_OPTION_METRIC_GEX_INTRADAY,
    TABLE_OPTION_METRIC_GEX_LEVELS_DAILY,
    TABLE_OPTION_SURFACE_IV_DAILY,
    TABLE_OPTION_FLOW_SENTIMENT_DAILY,
    TABLE_OPTION_FLOW_MULTI_LEG_DAILY,
    TABLE_OPTION_IV_RECONSTRUCTED_DAILY,
    TABLE_STOCK_SIGNAL_MOMENTUM_DAILY,
    TABLE_STOCK_SIGNAL_SEPA_DAILY,
    TABLE_STOCK_SIGNAL_VRP_DAILY,
    TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY,
    TABLE_EVENT_SIGNAL_RADAR_DAILY,
    TABLE_STOCK_FORECAST_TERRAIN_DAILY,
    TABLE_STOCK_FORECAST_TERRAIN_INTRADAY,
    TABLE_STOCK_FORECAST_SESSION,
    TABLE_STOCK_FORECAST_HOURLY,
    TABLE_STOCK_BACKTEST_SETTLEMENT,
    TABLE_STOCK_BACKTEST_RESULTS_PERIOD,
    TABLE_STOCK_SIGNAL_PLAYBOOK_TRIGGER_INTRADAY,
    TABLE_STOCK_SIGNAL_SCAN_DAILY,
    TABLE_STOCK_SIGNAL_LENS_HIT_DAILY,
    TABLE_STOCK_SIGNAL_ALERT_DAILY,
)

# Deprecated aliases — same canonical tables (gradual code migration)
TABLE_MOMENTUM_SCORE_DAILY = TABLE_STOCK_SIGNAL_MOMENTUM_DAILY
TABLE_SEPA_SCORE_DAILY = TABLE_STOCK_SIGNAL_SEPA_DAILY
TABLE_EVENT_RADAR = TABLE_EVENT_SIGNAL_RADAR_DAILY
TABLE_MARKET_TERRAIN_DAILY = TABLE_STOCK_FORECAST_TERRAIN_DAILY
TABLE_TERRAIN_INTRADAY = TABLE_STOCK_FORECAST_TERRAIN_INTRADAY
TABLE_FORECAST_SESSION = TABLE_STOCK_FORECAST_SESSION
TABLE_FORECAST_HOURLY = TABLE_STOCK_FORECAST_HOURLY
TABLE_FORECAST_SETTLEMENT = TABLE_STOCK_BACKTEST_SETTLEMENT
TABLE_BACKTEST_RESULTS = TABLE_STOCK_BACKTEST_RESULTS_PERIOD
