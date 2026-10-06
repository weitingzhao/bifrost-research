"""The Dagster schedule roster, readable without Dagster installed.

research-api runs the ``base`` image, which has no ``dagster`` package, so it
cannot import ``orchestration.definitions``. This table is the copy it reads;
``tests/orchestration/test_definitions.py`` fails when any field differs from
the ScheduleDefinitions (name, job, timezone, cron) or from the market slot
assets (slots). Everything else that names a schedule — Ops Console, the
liveness metrics, ``scripts/verify_husbandry_schedulers.sh`` — reads it through
``GET /research/orchestration/status`` or ``GET /metrics`` instead of keeping a
list of its own (TD-108: three hand-kept lists had drifted).

D10 BLOCKED.
"""

from __future__ import annotations

from typing import NamedTuple


class ScheduleSpec(NamedTuple):
    name: str
    job: str
    # ScheduleDefinition.execution_timezone. The instigator row in ops_dagster
    # carries the cron but not the zone.
    tz: str
    cron: str
    # Market Plugin slots the job enqueues on this cron (POST
    # /market/ingest/enqueue-slot). Empty for jobs that enqueue nothing.
    market_slots: tuple[str, ...] = ()


SCHEDULE_ROSTER: tuple[ScheduleSpec, ...] = (
    ScheduleSpec(
        "research_trading_day_schedule",
        "research_trading_day",
        "America/New_York",
        "30 22 * * 1-5",
    ),
    ScheduleSpec(
        "research_flex_morning_schedule",
        "research_flex_morning",
        "America/New_York",
        "30 6 * * 1-6",
    ),
    ScheduleSpec(
        "research_canonical_pnl_schedule",
        "research_canonical_pnl_job",
        "UTC",
        "40 23 * * 1-5",
    ),
    ScheduleSpec(
        "market_snapshot_schedule",
        "market_snapshot_job",
        "UTC",
        "5 21 * * *",
        ("stock-snapshot",),
    ),
    ScheduleSpec(
        "market_movers_schedule",
        "market_movers_job",
        "UTC",
        "10 21 * * *",
        ("stock-movers",),
    ),
    ScheduleSpec(
        "market_reference_schedule",
        "market_reference_job",
        "UTC",
        "30 21 * * *",
        ("reference",),
    ),
    ScheduleSpec(
        "market_universe_calendar_schedule",
        "market_universe_calendar_job",
        "UTC",
        "0 22 * * *",
        ("universe-daily", "calendar", "stock-eod", "eod-pipeline"),
    ),
    ScheduleSpec(
        "market_related_schedule",
        "market_related_job",
        "UTC",
        "30 22 * * *",
        ("related-rotate",),
    ),
    ScheduleSpec(
        "market_option_bars_schedule",
        "market_option_bars_job",
        "UTC",
        "45 22 * * *",
        ("option-bars",),
    ),
    ScheduleSpec(
        "market_corporate_schedule",
        "market_corporate_job",
        "UTC",
        "0 23 * * *",
        ("corporate",),
    ),
    ScheduleSpec(
        "market_minute_bars_schedule",
        "market_minute_bars_job",
        "UTC",
        "15 23 * * *",
        ("minute-bars",),
    ),
    ScheduleSpec(
        "market_fundamentals_rotate_schedule",
        "market_fundamentals_rotate_job",
        "UTC",
        "0 3 * * *",
        ("fundamentals-rotate",),
    ),
    ScheduleSpec(
        "market_fundamentals_market_schedule",
        "market_fundamentals_market_job",
        "UTC",
        "30 4 * * 2-6",
        ("fundamentals-market",),
    ),
    ScheduleSpec(
        "market_ratios_market_schedule",
        "market_ratios_market_job",
        "UTC",
        "10 5-8,11,14,20 * * *",
        ("ratios-market",),
    ),
    ScheduleSpec(
        "market_option_depth_schedule",
        "market_option_depth_job",
        "UTC",
        "30 7 * * 0",
        ("option-depth",),
    ),
    ScheduleSpec(
        "market_option_refresh_schedule",
        "market_option_refresh_job",
        "UTC",
        "20 */6 * * *",
        ("option-refresh",),
    ),
    ScheduleSpec("market_trim_schedule", "market_trim_job", "UTC", "15 2 * * *", ("trim",)),
    ScheduleSpec("market_self_heal_schedule", "market_self_heal_job", "UTC", "45 0 * * 2-6"),
    ScheduleSpec("market_self_heal_late_schedule", "market_self_heal_job", "UTC", "30 5 * * 2-6"),
    ScheduleSpec(
        "market_treasury_schedule",
        "market_treasury_job",
        "UTC",
        "0 12,23 * * 1-5",
        ("treasury",),
    ),
    ScheduleSpec(
        "market_ticker_details_schedule",
        "market_ticker_details_job",
        "UTC",
        "30 3 * * *",
        ("ticker-details",),
    ),
    ScheduleSpec(
        "market_corporate_backfill_schedule",
        "market_corporate_backfill_job",
        "UTC",
        "0 7 1 * *",
        ("corporate-backfill",),
    ),
    ScheduleSpec(
        "market_intraday_chain_1030_schedule",
        "market_intraday_chain_job",
        "America/New_York",
        "30 10 * * 1-5",
        ("intraday-chain",),
    ),
    ScheduleSpec(
        "market_intraday_chain_1300_schedule",
        "market_intraday_chain_job",
        "America/New_York",
        "0 13 * * 1-5",
        ("intraday-chain",),
    ),
    ScheduleSpec(
        "market_intraday_chain_1530_schedule",
        "market_intraday_chain_job",
        "America/New_York",
        "30 15 * * 1-5",
        ("intraday-chain",),
    ),
    ScheduleSpec("research_opex_schedule", "research_opex_job", "UTC", "30 23 * * 1-5"),
    ScheduleSpec(
        "research_vol_surface_svi_schedule",
        "research_vol_surface_svi_job",
        "UTC",
        "20 23 * * 1-5",
    ),
    ScheduleSpec("research_iv_solver_schedule", "research_iv_solver_job", "UTC", "25 23 * * 1-5"),
    ScheduleSpec("research_settlement_schedule", "research_settlement_job", "UTC", "45 23 * * 1-5"),
    ScheduleSpec(
        "research_forecast_schedule",
        "research_forecast_job",
        "America/New_York",
        "0 23 * * 1-5",
    ),
    ScheduleSpec(
        "research_intraday_schedule",
        "research_intraday_job",
        "America/New_York",
        "45 10-16 * * 1-5",
    ),
    ScheduleSpec(
        "research_event_radar_schedule",
        "research_event_radar_job",
        "UTC",
        "*/30 * * * *",
    ),
    ScheduleSpec(
        "research_macro_calendar_schedule",
        "research_macro_calendar_job",
        "UTC",
        "0 10 * * 1",
    ),
    ScheduleSpec(
        "research_daily_digest_schedule",
        "research_daily_digest_job",
        "UTC",
        "30 11 * * 1-5",
    ),
    ScheduleSpec(
        "research_weekly_policy_review_schedule",
        "research_weekly_policy_review_job",
        "UTC",
        "0 22 * * 0",
    ),
    ScheduleSpec("research_eod_review_schedule", "research_eod_review_job", "UTC", "30 21 * * 1-5"),
    ScheduleSpec(
        "research_memory_distill_schedule",
        "research_memory_distill_job",
        "UTC",
        "55 23 * * 1-5",
    ),
    ScheduleSpec(
        "research_ensure_partitions_schedule",
        "research_ensure_partitions_job",
        "UTC",
        "30 0 1 * *",
    ),
    ScheduleSpec(
        "research_vol_weekly_backfill_schedule",
        "research_vol_weekly_backfill_job",
        "UTC",
        "0 22 * * 0",
    ),
)

# (schedule_name, job name in ops_dagster.runs, execution_timezone)
HUSBANDRY_SCHEDULE_JOBS: tuple[tuple[str, str, str], ...] = tuple(
    (s.name, s.job, s.tz) for s in SCHEDULE_ROSTER
)

ROSTER_BY_NAME: dict[str, ScheduleSpec] = {s.name: s for s in SCHEDULE_ROSTER}

__all__ = ["HUSBANDRY_SCHEDULE_JOBS", "ROSTER_BY_NAME", "SCHEDULE_ROSTER", "ScheduleSpec"]
