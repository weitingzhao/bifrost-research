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
    Backoff,
    DefaultScheduleStatus,
    MaterializeResult,
    RetryPolicy,
    ScheduleDefinition,
    asset,
    define_asset_job,
)

from bifrost_research.orchestration.plugin_http import enqueue_market_slots

GROUP = "plugin_market_schedule"

# One enqueue is one HTTP call to the Plugin API. A dropped connection (the
# 2026-09-05 gap-heal: RemoteDisconnected after 12s) used to fail the run for
# good and nothing re-fired until the next week; three retries with backoff
# cover a pod restart or a slow enqueue without a human.
ENQUEUE_RETRY = RetryPolicy(max_retries=3, delay=60, backoff=Backoff.EXPONENTIAL)


def _make_slot_asset(asset_name: str, slots: tuple[str, ...], description: str):
    def _impl(context: AssetExecutionContext) -> MaterializeResult:
        return enqueue_market_slots(context, slots)

    _impl.__name__ = asset_name
    return asset(
        key=AssetKey(["batch", "market", asset_name]),
        group_name=GROUP,
        description=description,
        retry_policy=ENQUEUE_RETRY,
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
    "UTC 21:30 — reference",
)
market_universe_calendar = _make_slot_asset(
    "market_universe_calendar",
    ("universe-daily", "calendar", "stock-eod", "eod-pipeline"),
    "UTC 22:00 (~17:00 America/Chicago) — universe-daily + calendar + "
    "stock-eod + eod-pipeline (market_eod on research_trading_day remains catch-up)",
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
# oi-gap-heal retired on 2026-09-08 (P3): open interest now comes from the
# chain snapshot keyed to the session, so there is no gap left to heal.
# option-trades left this asset on 2026-09-06: Options Starter has no trades
# entitlement, so the Plugin retired the slot (it answers skipped, not failed)
# until the subscription is upgraded. corporate is now a whole-market pull.
market_corporate = _make_slot_asset(
    "market_corporate",
    ("corporate",),
    "UTC 23:00 — corporate actions (dividends + splits, whole market)",
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
# Financials & Ratios, whole market by date: ratios + short volume for the
# last completed session, short interest for the latest settlement.
market_fundamentals_market = _make_slot_asset(
    "market_fundamentals_market",
    ("fundamentals-market",),
    "UTC 04:30 Tue–Sat — ratios + short data for the last session (whole market)",
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
# P4 — several observations a session. The plugin keys each row to the instant
# it was taken, so these sit beside the 16:00 EOD row. New York time, not UTC:
# 10:30 ET is a market clock, and DST would drift a fixed UTC cron by an hour.
market_intraday_chain = _make_slot_asset(
    "market_intraday_chain",
    ("intraday-chain",),
    "10:30 / 13:00 / 15:30 America/New_York — intraday option chain snapshots",
)
market_treasury = _make_slot_asset(
    "market_treasury",
    ("treasury",),
    "UTC 12:00 / 23:00 — Treasury constant-maturity yields (risk-free leg for option models)",
)
market_ticker_details = _make_slot_asset(
    "market_ticker_details",
    ("ticker-details",),
    "UTC 03:30 — ticker overview fields (list_date / sector / market_cap), 200 a day",
)

MARKET_SCHEDULE_ASSETS = [
    market_snapshot,
    market_movers,
    market_reference,
    market_universe_calendar,
    market_related,
    market_option_bars,
    market_corporate,
    market_minute_bars,
    market_fundamentals_rotate,
    market_fundamentals_market,
    market_option_refresh,
    market_trim,
    market_intraday_chain,
    market_treasury,
    market_ticker_details,
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
        "universe-daily+calendar+stock-eod+eod-pipeline",
    ),
    ("market_related_schedule", "market_related_job", market_related, "30 22 * * *", "related-rotate"),
    ("market_option_bars_schedule", "market_option_bars_job", market_option_bars, "45 22 * * *", "option-bars"),
    (
        "market_corporate_schedule",
        "market_corporate_job",
        market_corporate,
        "0 23 * * *",
        "corporate (whole market)",
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
        "market_fundamentals_market_schedule",
        "market_fundamentals_market_job",
        market_fundamentals_market,
        "30 4 * * 2-6",
        "fundamentals-market",
    ),
    (
        "market_option_refresh_schedule",
        "market_option_refresh_job",
        market_option_refresh,
        "20 */6 * * *",
        "option-refresh",
    ),
    ("market_trim_schedule", "market_trim_job", market_trim, "15 2 * * *", "trim"),
    # Twice a day. The vendor's publication hour is not measurable from what we
    # keep: `treasury_yield.fetched_at` is a last-write column, because the
    # slot's 30-day lookback rewrites the whole window on every run, so "when did
    # this row first become available" is gone. Measured 2026-09-11: our newest
    # was 2026-09-08 while Polygon already held 09-09, so the 12:00 run had
    # simply preceded its arrival. A second late poll costs one page of twenty
    # rows and removes the need to guess the hour correctly.
    # Reference data, every day. /v3/reference/tickers/{ticker} is the only
    # source of list_date, sector, market_cap and description; the list endpoint
    # carries none of them, which is why list_date was null for all 5,317 active
    # tickers. 200 a day drains that in about four weeks and then keeps
    # refreshing the stalest. No weekday restriction — a listing date does not
    # depend on the market being open.
    (
        "market_ticker_details_schedule",
        "market_ticker_details_job",
        market_ticker_details,
        "30 3 * * *",
        "ticker-details",
    ),
    (
        "market_treasury_schedule",
        "market_treasury_job",
        market_treasury,
        "0 12,23 * * 1-5",
        "treasury yields",
    ),
]

# The three intraday fires share one job; only the clock differs.
_INTRADAY_FIRES: tuple[tuple[str, str], ...] = (
    ("market_intraday_chain_1030_schedule", "30 10 * * 1-5"),
    ("market_intraday_chain_1300_schedule", "0 13 * * 1-5"),
    ("market_intraday_chain_1530_schedule", "30 15 * * 1-5"),
)

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

market_intraday_chain_job = define_asset_job(
    name="market_intraday_chain_job",
    selection=AssetSelection.assets(market_intraday_chain),
    description="Massive intraday chain snapshot (America/New_York)",
)
MARKET_SCHEDULE_JOBS.append(market_intraday_chain_job)
for _sched_name, _cron in _INTRADAY_FIRES:
    MARKET_SCHEDULES.append(
        ScheduleDefinition(
            name=_sched_name,
            job=market_intraday_chain_job,
            cron_schedule=_cron,
            execution_timezone="America/New_York",
            default_status=DefaultScheduleStatus.RUNNING,
            description="Massive intraday chain snapshot (America/New_York)",
        )
    )
