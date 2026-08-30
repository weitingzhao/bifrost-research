"""Market Plugin slot enqueue schedules — UTC cadence matching former CronJobs.

Group ``plugin_market_schedule`` is excluded from ``research_trading_day``.
Workers remain executors via POST /market/ingest/enqueue-slot. D10 BLOCKED.

Note: do not use ``from __future__ import annotations`` — Dagster needs live context types.
"""

from typing import Any

from dagster import (
    AssetExecutionContext,
    AssetKey,
    AssetSelection,
    DefaultScheduleStatus,
    MaterializeResult,
    ScheduleDefinition,
    asset,
    define_asset_job,
)

from bifrost_research.orchestration.plugin_http import enqueue_market_slots

GROUP = "plugin_market_schedule"


def _make_slot_asset(asset_name: str, slots: tuple[str, ...], description: str):
    def _impl(context: AssetExecutionContext) -> MaterializeResult:
        return enqueue_market_slots(context, slots)

    _impl.__name__ = asset_name
    return asset(
        key=AssetKey(["batch", "market", asset_name]),
        group_name=GROUP,
        description=description,
    )(_impl)


def _make_schedule(
    *,
    schedule_name: str,
    job_name: str,
    asset_def: Any,
    cron: str,
    description: str,
) -> tuple[Any, ScheduleDefinition]:
    job = define_asset_job(
        name=job_name,
        selection=AssetSelection.assets(asset_def),
        description=description,
    )
    sched = ScheduleDefinition(
        name=schedule_name,
        job=job,
        cron_schedule=cron,
        execution_timezone="UTC",
        default_status=DefaultScheduleStatus.RUNNING,
        description=description,
    )
    return job, sched


# Wave 1 — session UTC
market_snapshot = _make_slot_asset(
    "market_snapshot",
    ("stock-snapshot",),
    "UTC 21:05 — stock-snapshot (former market-data-stock-snapshot Cron)",
)
market_movers = _make_slot_asset(
    "market_movers",
    ("stock-movers",),
    "UTC 21:10 — stock-movers",
)
market_reference = _make_slot_asset(
    "market_reference",
    ("reference",),
    "UTC 21:30 — reference (stock-eod remains on research_trading_day)",
)
market_universe_calendar = _make_slot_asset(
    "market_universe_calendar",
    ("universe-daily", "calendar"),
    "UTC 22:00 — universe-daily + calendar (eod-pipeline on research_trading_day)",
)
market_related = _make_slot_asset(
    "market_related",
    ("related-rotate",),
    "UTC 22:30 — related-rotate",
)
market_option_bars = _make_slot_asset(
    "market_option_bars",
    ("option-bars",),
    "UTC 22:45 — option-bars",
)
market_corporate_trades = _make_slot_asset(
    "market_corporate_trades",
    ("corporate", "option-trades"),
    "UTC 23:00 — corporate + option-trades",
)
market_minute_bars = _make_slot_asset(
    "market_minute_bars",
    ("minute-bars",),
    "UTC 23:15 — minute-bars",
)

# Wave 2 — rotate
market_fundamentals_rotate = _make_slot_asset(
    "market_fundamentals_rotate",
    ("fundamentals-rotate",),
    "UTC 03:00 — fundamentals-rotate (large financials batch)",
)

# Wave 3 — refresh + maintenance
market_option_refresh = _make_slot_asset(
    "market_option_refresh",
    ("option-refresh",),
    "UTC */6 at :20 — option-refresh",
)
market_trim = _make_slot_asset(
    "market_trim",
    ("trim",),
    "UTC 02:15 — trim / maintenance",
)
market_oi_gap_heal = _make_slot_asset(
    "market_oi_gap_heal",
    ("oi-gap-heal",),
    "UTC Sat 04:00 — oi-gap-heal",
)

MARKET_SCHEDULE_ASSETS = [
    market_snapshot,
    market_movers,
    market_reference,
    market_universe_calendar,
    market_related,
    market_option_bars,
    market_corporate_trades,
    market_minute_bars,
    market_fundamentals_rotate,
    market_option_refresh,
    market_trim,
    market_oi_gap_heal,
]

_MARKET_SPECS: list[tuple[str, str, Any, str, str]] = [
    ("market_snapshot_schedule", "market_snapshot_job", market_snapshot, "5 21 * * *", "stock-snapshot"),
    ("market_movers_schedule", "market_movers_job", market_movers, "10 21 * * *", "stock-movers"),
    ("market_reference_schedule", "market_reference_job", market_reference, "30 21 * * *", "reference"),
    (
        "market_universe_calendar_schedule",
        "market_universe_calendar_job",
        market_universe_calendar,
        "0 22 * * *",
        "universe-daily+calendar",
    ),
    ("market_related_schedule", "market_related_job", market_related, "30 22 * * *", "related-rotate"),
    ("market_option_bars_schedule", "market_option_bars_job", market_option_bars, "45 22 * * *", "option-bars"),
    (
        "market_corporate_trades_schedule",
        "market_corporate_trades_job",
        market_corporate_trades,
        "0 23 * * *",
        "corporate+option-trades",
    ),
    ("market_minute_bars_schedule", "market_minute_bars_job", market_minute_bars, "15 23 * * *", "minute-bars"),
    (
        "market_fundamentals_rotate_schedule",
        "market_fundamentals_rotate_job",
        market_fundamentals_rotate,
        "0 3 * * *",
        "fundamentals-rotate",
    ),
    (
        "market_option_refresh_schedule",
        "market_option_refresh_job",
        market_option_refresh,
        "20 */6 * * *",
        "option-refresh",
    ),
    ("market_trim_schedule", "market_trim_job", market_trim, "15 2 * * *", "trim"),
    (
        "market_oi_gap_heal_schedule",
        "market_oi_gap_heal_job",
        market_oi_gap_heal,
        "0 4 * * 6",
        "oi-gap-heal",
    ),
]

MARKET_SCHEDULE_JOBS: list[Any] = []
MARKET_SCHEDULES: list[ScheduleDefinition] = []
for sched_name, job_name, asset_def, cron, label in _MARKET_SPECS:
    job, sched = _make_schedule(
        schedule_name=sched_name,
        job_name=job_name,
        asset_def=asset_def,
        cron=cron,
        description=f"Massive {label} (UTC) — Cron suspended after migrate",
    )
    MARKET_SCHEDULE_JOBS.append(job)
    MARKET_SCHEDULES.append(sched)
