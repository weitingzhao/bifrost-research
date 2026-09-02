"""Research auxiliary schedules — former CronJobs (Wave 4–5).

Groups excluded from ``research_trading_day``. D10 BLOCKED.

Note: do not use ``from __future__ import annotations``.
"""

from typing import Any, Callable

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

from bifrost_research.orchestration.plugin_http import meta
from bifrost_research.orchestration import runners
from bifrost_research.orchestration.engine_assets import forecast as engines_forecast
from bifrost_research.scheduler import engines as engine_sched

GROUP_SIGNALS = "research_signals"
GROUP_INTRADAY = "research_intraday"
GROUP_AGENTS = "research_agents"
GROUP_MAINT = "research_maintenance"


def _run_asset(
    *,
    key_path: list[str],
    group: str,
    description: str,
    fn: Callable[[], dict[str, Any]],
):
    asset_name = key_path[-1]

    def _impl(context: AssetExecutionContext) -> MaterializeResult:
        context.log.info("run %s", asset_name)
        result = fn()
        context.log.info("%s result=%s", asset_name, result)
        return MaterializeResult(metadata=meta(result if isinstance(result, dict) else {}))

    _impl.__name__ = asset_name
    return asset(
        key=AssetKey(key_path),
        group_name=group,
        description=description,
    )(_impl)


# Wave 4 — daily signals
engines_vrp = _run_asset(
    key_path=["engines", "vrp"],
    group=GROUP_SIGNALS,
    description="VRP daily (former research-vrp Cron)",
    fn=lambda: __import__(
        "bifrost_research.engines.vrp.entry", fromlist=["run"]
    ).run(),
)
engines_opex = _run_asset(
    key_path=["engines", "opex_cycle"],
    group=GROUP_SIGNALS,
    description="OpEx cycle (former research-opex-cycle Cron)",
    fn=lambda: __import__(
        "bifrost_research.engines.opex_cycle.entry", fromlist=["run"]
    ).run(),
)
engines_vol_surface_svi = _run_asset(
    key_path=["engines", "vol_surface_svi"],
    group=GROUP_SIGNALS,
    description="SVI vol surface fit/residual (≠ iv-surface)",
    fn=lambda: __import__(
        "bifrost_research.engines.vol_surface.entry", fromlist=["run"]
    ).run(),
)
engines_alert_scan = _run_asset(
    key_path=["engines", "alert_scan"],
    group=GROUP_SIGNALS,
    description="Alert scan (former research-alert-scan)",
    fn=lambda: __import__(
        "bifrost_research.engines.alert_scan.entry", fromlist=["run"]
    ).run(),
)
engines_signal_hit = _run_asset(
    key_path=["engines", "signal_hit"],
    group=GROUP_SIGNALS,
    description="Lens hit-rate (former research-signal-hit)",
    fn=lambda: __import__(
        "bifrost_research.engines.signal_hit.entry", fromlist=["run"]
    ).run(),
)
engines_settlement = _run_asset(
    key_path=["engines", "settlement"],
    group=GROUP_SIGNALS,
    description="Forecast settlement rows (true source; not backtest scaffold)",
    fn=lambda: engine_sched.run_slot("settlement"),
)

# Re-use existing canonical_pnl asset from engine_assets — schedule it separately.
# Wave 5
engines_terrain_intraday = _run_asset(
    key_path=["engines", "terrain_intraday"],
    group=GROUP_INTRADAY,
    description="Terrain intraday",
    fn=lambda: engine_sched.run_slot("terrain-intraday"),
)
engines_gex_intraday = _run_asset(
    key_path=["engines", "gex_intraday"],
    group=GROUP_INTRADAY,
    description="GEX intraday",
    fn=lambda: engine_sched.run_slot("gex-intraday"),
)
engines_event_radar_sched = _run_asset(
    key_path=["engines", "event_radar_cron"],
    group=GROUP_INTRADAY,
    description="Event radar file ingest (*/30); distinct key from excluded ai_forecast asset",
    fn=lambda: runners.run_event_radar(),
)


def _run_morning_prep_agent() -> dict[str, Any]:
    from bifrost_research.copilot.agents.morning_prep import run_morning_prep

    out = run_morning_prep()
    return out if isinstance(out, dict) else {"ok": True, "engine": "morning_prep", "advisory": "D10 BLOCKED"}


def _run_eod_review_agent() -> dict[str, Any]:
    from bifrost_research.copilot.agents.eod_review import run_eod_review

    out = run_eod_review()
    return out if isinstance(out, dict) else {"ok": True, "engine": "eod_review", "advisory": "D10 BLOCKED"}


def _ensure_partitions() -> dict[str, Any]:
    from bifrost_research.db.conn import connect
    from bifrost_research.schema.ddl import ensure_month_partitions

    conn = connect()
    try:
        ensure_month_partitions(conn, months_back=3, months_forward=4)
        return {"engine": "ensure_partitions", "ok": True, "advisory": "D10 BLOCKED"}
    finally:
        conn.close()


agents_morning_prep = _run_asset(
    key_path=["agents", "morning_prep"],
    group=GROUP_AGENTS,
    description="Morning prep agent",
    fn=_run_morning_prep_agent,
)
agents_eod_review = _run_asset(
    key_path=["agents", "eod_review"],
    group=GROUP_AGENTS,
    description="EOD review agent",
    fn=_run_eod_review_agent,
)

maint_ensure_partitions = _run_asset(
    key_path=["maintenance", "ensure_partitions"],
    group=GROUP_MAINT,
    description="Monthly partition ensure",
    fn=_ensure_partitions,
)


def _run_vol_weekly_backfill() -> dict[str, Any]:
    return runners.run_volatility(lookback_days=90)


maint_vol_weekly_backfill = _run_asset(
    key_path=["maintenance", "vol_weekly_backfill"],
    group=GROUP_MAINT,
    description="Sunday volatility 90d backfill",
    fn=_run_vol_weekly_backfill,
)

RESEARCH_AUX_ASSETS = [
    engines_vrp,
    engines_opex,
    engines_vol_surface_svi,
    engines_alert_scan,
    engines_signal_hit,
    engines_settlement,
    engines_terrain_intraday,
    engines_gex_intraday,
    engines_event_radar_sched,
    agents_morning_prep,
    agents_eod_review,
    maint_ensure_partitions,
    maint_vol_weekly_backfill,
]


def _job_sched(
    *,
    schedule_name: str,
    job_name: str,
    assets: list[Any],
    cron: str,
    tz: str,
    description: str,
) -> tuple[Any, ScheduleDefinition]:
    job = define_asset_job(
        name=job_name,
        selection=AssetSelection.assets(*assets),
        description=description,
    )
    sched = ScheduleDefinition(
        name=schedule_name,
        job=job,
        cron_schedule=cron,
        execution_timezone=tz,
        default_status=DefaultScheduleStatus.RUNNING,
        description=description,
    )
    return job, sched


RESEARCH_AUX_JOBS: list[Any] = []
RESEARCH_AUX_SCHEDULES: list[ScheduleDefinition] = []

_specs: list[tuple[str, str, list[Any], str, str, str]] = [
    ("research_vrp_schedule", "research_vrp_job", [engines_vrp], "10 23 * * 1-5", "UTC", "VRP"),
    ("research_opex_schedule", "research_opex_job", [engines_opex], "30 23 * * 1-5", "UTC", "OpEx"),
    (
        "research_vol_surface_svi_schedule",
        "research_vol_surface_svi_job",
        [engines_vol_surface_svi],
        "20 23 * * 1-5",
        "UTC",
        "SVI surface",
    ),
    (
        "research_alert_scan_schedule",
        "research_alert_scan_job",
        [engines_alert_scan],
        "30 22 * * 1-5",
        "UTC",
        "alert-scan",
    ),
    (
        "research_signal_hit_schedule",
        "research_signal_hit_job",
        [engines_signal_hit],
        "10 0 * * 1-6",
        "UTC",
        "signal-hit",
    ),
    (
        "research_settlement_schedule",
        "research_settlement_job",
        [engines_settlement],
        "0 22 * * 1-5",
        "UTC",
        "settlement",
    ),
    (
        "research_intraday_schedule",
        "research_intraday_job",
        [engines_terrain_intraday, engines_gex_intraday],
        "30 14-20 * * 1-5",
        "UTC",
        "intraday terrain+gex",
    ),
    (
        "research_event_radar_schedule",
        "research_event_radar_job",
        [engines_event_radar_sched],
        "*/30 * * * 1-5",
        "UTC",
        "event-radar",
    ),
    (
        "research_morning_prep_schedule",
        "research_morning_prep_job",
        [agents_morning_prep],
        "30 11 * * 1-5",
        "UTC",
        "morning-prep",
    ),
    (
        "research_eod_review_schedule",
        "research_eod_review_job",
        [agents_eod_review],
        "30 21 * * 1-5",
        "UTC",
        "eod-review",
    ),
    (
        "research_ensure_partitions_schedule",
        "research_ensure_partitions_job",
        [maint_ensure_partitions],
        "30 0 1 * *",
        "UTC",
        "ensure-partitions",
    ),
    (
        "research_vol_weekly_backfill_schedule",
        "research_vol_weekly_backfill_job",
        [maint_vol_weekly_backfill],
        "0 22 * * 0",
        "UTC",
        "vol-weekly-backfill",
    ),
    # engines/forecast writes features.stock_forecast_session — the only input
    # run_settlement has. It is excluded from research_trading_day twice over
    # (group ai_forecast, then by key) and belonged to no schedule, so it had no
    # producer at all: one ad-hoc materialization on 2026-08-30 and nothing since.
    # Sessions stopped at 2026-08-28, settlement then had nothing left to settle,
    # and stock_backtest_settlement went stale while its schedule kept reporting
    # SUCCESS. Its own group cannot simply rejoin trading_day — event_radar and
    # backtest live there too and carry their own cadence.
    #
    # New York, 1-5, matching research_trading_day: this asset depends on
    # engines/terrain, which that job produces at 22:30 ET. Half an hour later
    # keeps the dependency ordered on the same calendar, and still lands well
    # before research_settlement_schedule at 22:00 UTC the following day.
    (
        "research_forecast_schedule",
        "research_forecast_job",
        [engines_forecast],
        "0 23 * * 1-5",
        "America/New_York",
        "forecast sessions",
    ),
]

for sched_name, job_name, assets, cron, tz, label in _specs:
    job, sched = _job_sched(
        schedule_name=sched_name,
        job_name=job_name,
        assets=assets,
        cron=cron,
        tz=tz,
        description=f"Research {label} — Cron suspended after migrate",
    )
    RESEARCH_AUX_JOBS.append(job)
    RESEARCH_AUX_SCHEDULES.append(sched)
