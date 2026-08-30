"""Trading-day + multi-schedule husbandry jobs.

``research_trading_day`` excludes plugin_market_schedule and research aux groups.
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
from bifrost_research.orchestration.market_slot_schedules import (
    MARKET_SCHEDULE_JOBS,
    MARKET_SCHEDULES,
)
from bifrost_research.orchestration.research_aux_schedules import (
    RESEARCH_AUX_JOBS,
    RESEARCH_AUX_SCHEDULES,
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
)

research_trading_day_job = define_asset_job(
    name="research_trading_day",
    selection=AssetSelection.all() - _EXCLUDED,
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
        "Mon–Fri after US close: enqueue Market EOD/Flex then Research OLAP. "
        "All overlapping husbandry CronJobs must stay suspended."
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
    research_canonical_pnl_job,
    *MARKET_SCHEDULE_JOBS,
    *RESEARCH_AUX_JOBS,
]
RESEARCH_SCHEDULES = [
    research_trading_day_schedule,
    research_canonical_pnl_schedule,
    *MARKET_SCHEDULES,
    *RESEARCH_AUX_SCHEDULES,
]
