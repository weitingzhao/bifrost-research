"""Trading-day + multi-schedule husbandry jobs.

``research_trading_day`` excludes plugin_market_schedule and research aux groups,
and since the Flex morning split also the two Flex enqueue assets: IB's Activity
statement is generated overnight, so Flex is fetched at 06:30 America/New_York
(``research_flex_morning``) and the trading-day gate checks its outcome.
All husbandry CronJobs must stay suspended after migrate. D10 BLOCKED.
"""

from dagster import (
    AssetKey,
    AssetSelection,
    DefaultScheduleStatus,
    ScheduleDefinition,
    define_asset_job,
)

from bifrost_research.orchestration.engine_assets import canonical_pnl
from bifrost_research.orchestration.market_self_heal import (
    market_self_heal_job,
    market_self_heal_schedule,
)
from bifrost_research.orchestration.market_slot_schedules import (
    MARKET_SCHEDULE_JOBS,
    MARKET_SCHEDULES,
)
from bifrost_research.orchestration.research_aux_schedules import (
    RESEARCH_AUX_JOBS,
    RESEARCH_AUX_SCHEDULES,
)

# Flex ingest: enqueued in the morning, once IB has generated the statement.
FLEX_MORNING_SELECTION = AssetSelection.assets(
    AssetKey(["batch", "flex_trades"]),
    AssetKey(["batch", "flex_transactions"]),
)

# Trading-day: core batch only — not market UTC slots / research aux schedules.
_EXCLUDED = (
    AssetSelection.groups(
        "external",
        "ai_forecast",
        "plugin_market_schedule",
        "research_signals",
        "research_intraday",
        "research_agents",
        "research_maintenance",
    )
    | AssetSelection.assets(
        AssetKey(["engines", "event_radar"]),
        AssetKey(["engines", "backtest"]),
        AssetKey(["engines", "canonical_pnl"]),
        AssetKey(["engines", "forecast"]),
        AssetKey(["engines", "event_radar_cron"]),
    )
    | FLEX_MORNING_SELECTION
)

# Runs launch inside the dagster-daemon container (DefaultRunLauncher), so every
# step subprocess shares its memory. Uncapped, the gate released dbt, five engines
# and option_universe at once; on 2026-09-11 that fan-out OOMKilled the container
# (2Gi) at 02:34:31 and the run died mid-dbt -- after dbt's CASCADE had dropped
# mart_sepa_criteria_stats, taking the Stock Screener down with it. The engines
# grew heavy with this week's backfills; three at a time costs minutes, not hours.
TRADING_DAY_MAX_CONCURRENT = 3

research_trading_day_job = define_asset_job(
    name="research_trading_day",
    selection=AssetSelection.all() - _EXCLUDED,
    config={
        "execution": {
            "config": {"multiprocess": {"max_concurrent": TRADING_DAY_MAX_CONCURRENT}}
        }
    },
    description=(
        "Trading-day batch: Plugin EOD enqueue → husbandry gate → "
        "dbt (if present) → SEPA projection → core engines + scan. D10 BLOCKED."
    ),
)

research_trading_day_schedule = ScheduleDefinition(
    name="research_trading_day_schedule",
    job=research_trading_day_job,
    cron_schedule="30 22 * * 1-5",
    execution_timezone="America/New_York",
    default_status=DefaultScheduleStatus.RUNNING,
    description=(
        "Mon–Fri after US close: enqueue Market EOD, gate on Market + Flex outcome, "
        "then Research OLAP. Flex itself is enqueued by research_flex_morning_schedule. "
        "All overlapping husbandry CronJobs must stay suspended."
    ),
)

# Flex morning: the plugin's worker waits for IB with deferred retries, so one
# enqueue per morning is enough. Saturday collects Friday.
research_flex_morning_job = define_asset_job(
    name="research_flex_morning",
    selection=FLEX_MORNING_SELECTION,
    description=(
        "Enqueue Flex trades + cash transactions after IB has generated the previous "
        "day's Activity statement. Mirrored by the plugin's config/schedule.yaml. D10 BLOCKED."
    ),
)
research_flex_morning_schedule = ScheduleDefinition(
    name="research_flex_morning_schedule",
    job=research_flex_morning_job,
    cron_schedule="30 6 * * 1-6",
    execution_timezone="America/New_York",
    default_status=DefaultScheduleStatus.RUNNING,
    description=(
        "06:30 America/New_York Mon–Sat: enqueue Flex ingest for the previous trading day. "
        "The plugin-flex-query CronJobs were removed in plugin 0.6.0; this is the trigger."
    ),
)

# Wave 4 — schedule existing canonical_pnl asset (excluded from trading_day).
research_canonical_pnl_job = define_asset_job(
    name="research_canonical_pnl_job",
    selection=AssetSelection.assets(canonical_pnl),
    description="Canonical PnL cohort (former research-canonical-pnl Cron)",
)
research_canonical_pnl_schedule = ScheduleDefinition(
    name="research_canonical_pnl_schedule",
    job=research_canonical_pnl_job,
    cron_schedule="40 23 * * 1-5",
    execution_timezone="UTC",
    default_status=DefaultScheduleStatus.RUNNING,
    description="Canonical PnL — Cron suspended after migrate",
)

RESEARCH_JOBS = [
    research_trading_day_job,
    research_flex_morning_job,
    research_canonical_pnl_job,
    *MARKET_SCHEDULE_JOBS,
    market_self_heal_job,
    *RESEARCH_AUX_JOBS,
]
RESEARCH_SCHEDULES = [
    research_trading_day_schedule,
    research_flex_morning_schedule,
    research_canonical_pnl_schedule,
    *MARKET_SCHEDULES,
    market_self_heal_schedule,
    *RESEARCH_AUX_SCHEDULES,
]
