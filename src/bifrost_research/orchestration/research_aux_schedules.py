"""Research auxiliary schedules — former CronJobs (Wave 4–5).

Groups excluded from ``research_trading_day``. D10 BLOCKED.

Note: do not use ``from __future__ import annotations``.
"""

from datetime import date
from typing import Any, Callable

from dagster import (
    AssetExecutionContext,
    AssetKey,
    AssetSelection,
    Config,
    DefaultScheduleStatus,
    MaterializeResult,
    ScheduleDefinition,
    asset,
    define_asset_job,
)

from bifrost_research.orchestration import runners
from bifrost_research.orchestration.asset_checks import (
    ERROR,
    GENERIC,
    WARN,
    OutputSpec,
    field,
    judged_materialization,
    output_check_specs,
)
from bifrost_research.orchestration.plugin_http import meta
from bifrost_research.orchestration.engine_assets import (
    VOLATILITY_SPEC,
    candidate_outcome as engines_candidate_outcome,
    forecast as engines_forecast,
)
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
    deps: list[AssetKey] | None = None,
    spec: OutputSpec | None = GENERIC,
):
    """``spec`` judges the result in the asset's ``output_ok`` check (TD-92); ``None``
    only for assets listed in ``asset_checks.OUTPUT_CHECK_OPT_OUT`` with a reason."""
    asset_name = key_path[-1]
    key = AssetKey(key_path)

    def _impl(context: AssetExecutionContext) -> MaterializeResult:
        context.log.info("run %s", asset_name)
        result = fn()
        context.log.info("%s result=%s", asset_name, result)
        payload = result if isinstance(result, dict) else {}
        if spec is None:
            return MaterializeResult(metadata=meta(payload))
        return judged_materialization(context, payload, meta(payload), spec)

    _impl.__name__ = asset_name
    return asset(
        key=key,
        group_name=group,
        description=description,
        deps=deps or None,
        check_specs=output_check_specs(key) if spec is not None else None,
    )(_impl)


# Engines that count names they could not compute (opex, SVI): no normal share has
# been measured, so a high one is a WARN until it has.
_SYMBOLS_SPEC = OutputSpec(
    rows=field("symbols_ok"),
    expect_rows=True,
    soft_failed=(("symbols_skipped", "symbols_ok"),),
)
# The agents' own ``ok: false`` / ``error`` are LLM or post failures: shown, not alerted.
_AGENT_SPEC = OutputSpec(error_severity=WARN)


def _alert_scan_judged_session(result: dict[str, Any]) -> list[tuple[Any, str]]:
    """TD-97: the newest date judged is the New York session the batch closed."""
    session = result.get("session")
    if session and result.get("as_of") != session:
        return [(ERROR, f"judged {result.get('as_of')}, the New York session is {session}")]
    return []


ALERT_SCAN_SPEC = OutputSpec(extra=_alert_scan_judged_session)


# Wave 4 daily-signal assets — VRP moved into the trading-day chain (engine_assets.vrp,
# after volatility) in research-loop-automation A4: at 23:10 UTC the day's ATM IV
# did not exist yet, so every day's latest VRP row was written with IV NULL.
engines_opex = _run_asset(
    key_path=["engines", "opex_cycle"],
    group=GROUP_SIGNALS,
    description="OpEx cycle (former research-opex-cycle Cron)",
    fn=lambda: __import__(
        "bifrost_research.engines.opex_cycle.entry", fromlist=["run"]
    ).run(),
    spec=_SYMBOLS_SPEC,
)
engines_vol_surface_svi = _run_asset(
    key_path=["engines", "vol_surface_svi"],
    group=GROUP_SIGNALS,
    description="SVI vol surface fit/residual (≠ iv-surface)",
    fn=lambda: __import__(
        "bifrost_research.engines.vol_surface.entry", fromlist=["run"]
    ).run(),
    spec=_SYMBOLS_SPEC,
)


def _run_iv_solver() -> dict[str, Any]:
    """Daily vendor IV projection → features.option_iv_reconstructed_daily.

    Snapshot only since 0.111.0. The table's one reader, ATM IV, solves Brent from
    option_daily in place for whatever the vendor rows lack (Trade's greeks route
    does the same on demand), so the stored Brent rows (~1.5k a day once vendor rows
    took precedence) were a second path to the same number — one that ATM read for
    the last five sessions and not before, and that could drift from it.

    SVI surface fit does **not** write this table; Console used to map
    ``iv_reconstructed`` freshness to research_vol_surface_svi by mistake, so the
    schedule kept reporting SUCCESS while computed_at froze. Short lookback is
    enough for the weekday 36h SLA; weekly vol backfill covers deeper history.
    """
    import json
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from bifrost_research.db.calendar import (
        load_symbols_from_env_or_query,
        union_iv_radar_benchmarks,
    )
    from bifrost_research.db.conn import connect
    from bifrost_research.engines.volatility.iv_solver import run_cohort

    as_of = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
    conn = connect()
    try:
        universe = union_iv_radar_benchmarks(load_symbols_from_env_or_query(conn))
        result = run_cohort(
            conn,
            symbols=universe,
            lookback_days=5,
            as_of=as_of,
            source="snapshot",
            dry_run=False,
        )
        # Compact for Dagster metadata (run_cohort returns a large per-symbol list).
        if isinstance(result, dict):
            return {
                "ok": True,
                "engine": "iv_solver",
                "as_of": as_of.isoformat(),
                "symbols": len(universe),
                # Not ``a or b``: 0 rows must reach the output check as 0 (TD-92).
                "rows_written": result.get("rows_written", result.get("total_rows_written")),
                "advisory": "D10 BLOCKED",
                "detail": json.dumps(result, default=str)[:400],
            }
        return {"ok": True, "engine": "iv_solver", "result": str(result)[:400]}
    finally:
        conn.close()


engines_iv_solver = _run_asset(
    key_path=["engines", "iv_solver"],
    group=GROUP_SIGNALS,
    description="Vendor snapshot IV projection → features.option_iv_reconstructed_daily (IDS)",
    fn=_run_iv_solver,
    spec=OutputSpec(rows=field("rows_written"), expect_rows=True),
)
# In research_trading_day, after engines/scan (TD-97). Its own 22:30 UTC schedule
# ran four hours before the scan it reads, so every session was judged the next
# evening from the previous night's scan; the ERROR check now asserts the judged
# date is the New York session the batch closed.
engines_alert_scan = _run_asset(
    key_path=["engines", "alert_scan"],
    group=GROUP_SIGNALS,
    description=(
        "Alert scan: composite_high re-judged on the last 5 scan dates, hit-rate "
        "drop and weight shift on the newest — in research_trading_day after scan"
    ),
    # hit_rate_drop / weight_shift read hit_5d, which signal_hit_fwd_fill fills
    # on rows whose five sessions have just elapsed (TD-156).
    deps=[AssetKey(["engines", "scan"]), AssetKey(["engines", "signal_hit_fwd_fill"])],
    fn=lambda: __import__(
        "bifrost_research.engines.alert_scan.entry", fromlist=["run"]
    ).run(),
    spec=ALERT_SCAN_SPEC,
)

#: The trading-day asset that writes each table a decay lens fires on
#: (signal_hit.entry.LENS_SOURCE). raw_market.stock_daily, the forward legs, is
#: the gate's; option_surface_fit_daily (skew) is SVI's, at 23:20 UTC before the
#: batch. tests/orchestration/test_judge_after_writer.py holds the edges.
SIGNAL_HIT_SOURCES: tuple[tuple[str, str], ...] = (
    ("features.option_metric_iv_percentile_daily", "engines/volatility"),
    ("features.option_metric_max_pain_daily", "engines/volatility"),
    ("features.stock_signal_vrp_daily", "engines/vrp"),
    ("features.option_metric_gex_levels_daily", "engines/gex"),
    ("features.stock_forecast_terrain_daily", "engines/terrain"),
    ("features.option_flow_sentiment_daily", "engines/flow"),
    ("features.stock_signal_momentum_daily", "engines/momentum"),
    ("features.stock_signal_sepa_daily", "features/sepa_projection"),
    ("raw_market.stock_daily", "batch/husbandry_gate"),
)


def _signal_hit_judged_session(result: dict[str, Any]) -> list[tuple[Any, str]]:
    """TD-156: the newest day walked is the New York session the batch closed."""
    session = result.get("session")
    if session and result.get("as_of") != session:
        return [(ERROR, f"walked to {result.get('as_of')}, the New York session is {session}")]
    return []


SIGNAL_HIT_SPEC = OutputSpec(
    rows=field("rows_written"), expect_rows=True, extra=_signal_hit_judged_session
)


# In research_trading_day, after every writer of a lens source (TD-156). Its own
# 00:10 UTC schedule fired two hours before the batch wrote the session's
# features, so each night walked the previous night's view of every lens.
engines_signal_hit = _run_asset(
    key_path=["engines", "signal_hit"],
    group=GROUP_SIGNALS,
    description=(
        "Lens hit-rate: the last 3 sessions re-walked — in research_trading_day "
        "after the features each lens fires on"
    ),
    deps=[AssetKey(w.split("/")) for w in dict.fromkeys(w for _t, w in SIGNAL_HIT_SOURCES)],
    fn=lambda: __import__(
        "bifrost_research.engines.signal_hit.entry", fromlist=["run"]
    ).run(),
    spec=SIGNAL_HIT_SPEC,
)
engines_settlement = _run_asset(
    key_path=["engines", "settlement"],
    group=GROUP_SIGNALS,
    description="Forecast settlement rows (true source; not backtest scaffold)",
    fn=lambda: engine_sched.run_slot("settlement"),
    spec=OutputSpec(rows=field("sessions_settled"), expect_rows=True),
)

# Re-use existing canonical_pnl asset from engine_assets — schedule it separately.
# Wave 5
engines_gex_intraday = _run_asset(
    key_path=["engines", "gex_intraday"],
    group=GROUP_INTRADAY,
    description="GEX intraday",
    fn=lambda: engine_sched.run_slot("gex-intraday"),
    spec=OutputSpec(soft_failed=(("symbols_failed", "symbols_ok"),)),
)
# After gex: a name the intraday chain observed stands on the GEX row this tick
# just wrote (0.137.0); run side by side it read the previous hour's.
engines_terrain_intraday = _run_asset(
    key_path=["engines", "terrain_intraday"],
    group=GROUP_INTRADAY,
    description="Terrain intraday",
    fn=lambda: engine_sched.run_slot("terrain-intraday"),
    deps=[AssetKey(["engines", "gex_intraday"])],
    spec=OutputSpec(
        rows=field("rows_written"),
        expect_rows=True,
        failed=(("trigger_failures", "trigger_ok"),),
        soft_failed=(("skipped_no_spot", "rows_written"),),
    ),
)
# SEC 8-K filings -> event radar (TD-100). Until 2026-10 this asset ran the file
# ingest against an input directory the pod never had, answered "idle" every
# 30 minutes, and the real feed ran from a tmux loop on the Owner's Mac. The
# Owner's own drop-zone files stay on the Mac (scripts/event_radar_watch.sh).
engines_event_radar_sched = _run_asset(
    key_path=["engines", "event_radar_cron"],
    group=GROUP_INTRADAY,
    description=(
        "SEC 8-K filings (raw_market.sec_8k_filing) -> features.event_signal_radar_daily, "
        "every 30 minutes; idempotent, raises on failure"
    ),
    fn=lambda: runners.run_event_radar_sec(),
    spec=None,
)


def _macro_horizon(result: dict[str, Any]) -> list[tuple[Any, str]]:
    from bifrost_research.scheduler.macro_ingest import horizon_findings

    return [(ERROR if level == "error" else WARN, text) for level, text in horizon_findings(result)]


# TD-151: macro_ingest had no caller, so features.macro_event_daily stayed at 0
# rows and the Events board's "Macro forward" panel was always empty. The source
# is the packaged calendar (no entitled vendor feed exists); the check is the
# reminder to extend it before it runs out.
engines_macro_calendar = _run_asset(
    key_path=["engines", "macro_calendar"],
    group=GROUP_SIGNALS,
    description=(
        "Forward macro calendar (scheduler/data/macro_calendar.csv via macro_ingest) -> "
        "features.macro_event_daily; ERROR when it reaches less than 30 days ahead"
    ),
    fn=lambda: runners.run_macro_calendar(),
    spec=OutputSpec(
        rows=field("rows_written"),
        expect_rows=True,
        trailing=False,
        extra=_macro_horizon,
    ),
)


def _run_morning_prep_agent() -> dict[str, Any]:
    from bifrost_research.copilot.agents.morning_prep import run_morning_prep

    out = run_morning_prep()
    return out if isinstance(out, dict) else {"ok": True, "engine": "morning_prep", "advisory": "D10 BLOCKED"}


def _run_daily_digest_agent() -> dict[str, Any]:
    from bifrost_research.copilot.agents.daily_digest import run_daily_digest

    out = run_daily_digest()
    return out if isinstance(out, dict) else {"ok": True, "engine": "daily_digest", "advisory": "D10 BLOCKED"}


def _run_weekly_policy_review_agent() -> dict[str, Any]:
    from bifrost_research.copilot.agents.weekly_policy_review import run_weekly_policy_review

    out = run_weekly_policy_review()
    return out if isinstance(out, dict) else {"ok": True, "engine": "weekly_policy_review", "advisory": "D10 BLOCKED"}


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
    spec=_AGENT_SPEC,
)
# D2: one digest per trading day — holdings ∪ new candidates through the lenses,
# loop state, resolutions and dissents since yesterday. Morning Prep's per-
# hypothesis posts fold into it; the asset stays for manual runs, its schedule
# does not.
agents_daily_digest = _run_asset(
    key_path=["agents", "daily_digest"],
    group=GROUP_AGENTS,
    description="Daily digest — one briefing per trading day (morning prep folded in)",
    fn=_run_daily_digest_agent,
    spec=_AGENT_SPEC,
)
# D3: once a week the rules get a proposal from settled outcomes — a
# policy_suggestion draft per objective whose record calls for a change.
agents_weekly_policy_review = _run_asset(
    key_path=["agents", "weekly_policy_review"],
    group=GROUP_AGENTS,
    description="Weekly policy review — policy_suggestion from settled candidate outcomes",
    fn=_run_weekly_policy_review_agent,
    spec=_AGENT_SPEC,
)
# B3: settle the candidates' forward windows before the review reads them, so
# a hypothesis whose window closed today is resolved today, not tomorrow.
agents_eod_review = _run_asset(
    key_path=["agents", "eod_review"],
    group=GROUP_AGENTS,
    description="EOD review agent — outcome rule first, drafts for the rest",
    fn=_run_eod_review_agent,
    deps=[AssetKey(["engines", "candidate_outcome"])],
    spec=_AGENT_SPEC,
)

def _run_journal_distill() -> dict:
    """K6 — the nightly memory distillation (journal.memory, Spec §20)."""
    from bifrost_research.db.conn import connect
    from bifrost_research.engines.journal_distill import run_distill

    conn = connect()
    try:
        return run_distill(conn)
    finally:
        conn.close()


# §20: after the close the trader's own trail (fills, notes, visits, Inbox
# answers) distils into journal.memory — measured, never invented.
agents_journal_distill = _run_asset(
    key_path=["agents", "journal_distill"],
    group=GROUP_AGENTS,
    description="Journal memory distill — the trader's own trail → journal.memory (§20)",
    fn=_run_journal_distill,
)

maint_ensure_partitions = _run_asset(
    key_path=["maintenance", "ensure_partitions"],
    group=GROUP_MAINT,
    description="Monthly partition ensure",
    fn=_ensure_partitions,
)


def _run_vol_weekly_backfill() -> dict[str, Any]:
    out = runners.run_volatility(lookback_days=90)
    out["coverage_heal"] = runners.run_iv_coverage_heal()
    # After the heal, never before: the re-walk reads what the heal rewrote.
    out["signal_hit_rewalk"] = runners.run_signal_hit_full_rewalk()
    return out


maint_vol_weekly_backfill = _run_asset(
    key_path=["maintenance", "vol_weekly_backfill"],
    group=GROUP_MAINT,
    description="Sunday volatility 90d backfill, IV coverage heal over two years, then signal-hit re-walk over the same span",
    fn=_run_vol_weekly_backfill,
    spec=VOLATILITY_SPEC,
)

# Materialised by hand, not on a schedule: it fills terrain for the sessions the
# Owner opened positions that are now closed, and reports every session whose
# inputs cannot support a regime rather than scoring one from defaults (R9 F3).
maint_terrain_backfill = _run_asset(
    key_path=["maintenance", "terrain_backfill"],
    group=GROUP_MAINT,
    description=(
        "Terrain on closed instances' opening sessions, only where GEX / momentum / IV "
        "really cover the date. Manual run; reports input floors, new_rows vs "
        "rewritten_rows, and coverage. Never overwrites a session the nightly slot "
        "already wrote — that needs runners.run_terrain_backfill(force=True)."
    ),
    fn=runners.run_terrain_backfill,
    spec=None,
)


#: Deep backfills are only offered for the two signal slots terrain waits on; a
#: config typo must not start a 60-day run of something else.
BACKFILLABLE_SLOTS = ("momentum", "gex")


class SignalBackfillConfig(Config):
    """Which signal slot to recompute, over which window.

    ``as_of`` ends the window (``YYYY-MM-DD``, default today), so a gap in the
    middle of the history can be filled without recomputing everything since:
    measured 2026-09-16, five days of GEX takes seven minutes and writes 450k
    rows, and rewriting rows the nightly slot already owns costs that for nothing.
    """

    slot: str = "momentum"
    lookback_days: int = 5
    as_of: str | None = None


# Manual: recompute a signal slot over a deep window so terrain has inputs on the
# sessions the Owner traded (R9 C2). The nightly slots only walk two days.
@asset(
    key=AssetKey(["maintenance", "signal_backfill"]),
    group_name=GROUP_MAINT,
    description=(
        "Recompute momentum or gex over a deep lookback (config: slot, lookback_days). "
        "Manual run — the nightly slots walk 2 trading days, which is why terrain has "
        "no inputs on older sessions. GEX cannot go earlier than option_open_interest."
    ),
)
def maint_signal_backfill(
    context: AssetExecutionContext, config: SignalBackfillConfig
) -> MaterializeResult:
    if config.slot not in BACKFILLABLE_SLOTS:
        raise ValueError(f"slot must be one of {BACKFILLABLE_SLOTS}: {config.slot!r}")
    as_of = date.fromisoformat(config.as_of) if config.as_of else None
    context.log.info(
        "run signal_backfill slot=%s lookback_days=%s as_of=%s",
        config.slot,
        config.lookback_days,
        as_of,
    )
    result = engine_sched.run_slot(
        config.slot, lookback_days=config.lookback_days, as_of=as_of
    )
    context.log.info("signal_backfill result=%s", result)
    return MaterializeResult(metadata=meta(result if isinstance(result, dict) else {}))


class EventRadarPurgeConfig(Config):
    """``force`` has to be typed in by hand before anything is deleted."""

    force: bool = False


# One-off, by hand, never on a schedule: it deletes the Event Radar rows that were
# never data (Owner authorised 2026-09-15, R9 C1). Default is a dry run.
@asset(
    key=AssetKey(["maintenance", "event_radar_purge"]),
    group_name=GROUP_MAINT,
    description=(
        "Delete the canned Event Radar rows (dagster-fallback, ws:*smoke*, ws:sample). "
        "Default config is a dry run reporting counts per source; set force: true to "
        "delete. The read endpoints keep excluding these sources afterwards."
    ),
)
def maint_event_radar_purge(
    context: AssetExecutionContext, config: EventRadarPurgeConfig
) -> MaterializeResult:
    context.log.info("run event_radar_purge force=%s", config.force)
    result = runners.run_event_radar_purge(force=config.force)
    context.log.info("event_radar_purge result=%s", result)
    return MaterializeResult(metadata=meta(result))


#: Assets that read tables research_trading_day writes, with each table and the
#: trading-day asset that writes it. Each runs inside research_trading_day
#: downstream of every writer, or on schedules that fire outside 20:00-02:30
#: UTC, when the batch has not yet written the session (TD-97: alert_scan at
#: 22:30 UTC judged the previous night's scan; TD-156: signal_hit at 00:10 UTC).
#: tests/orchestration/test_judge_after_writer.py holds every entry to that.
READS_TRADING_DAY_OUTPUT: dict[str, tuple[tuple[str, str], ...]] = {
    "engines/alert_scan": (
        ("features.stock_signal_scan_daily", "engines/scan"),
        ("features.stock_signal_lens_hit_daily", "engines/signal_hit_fwd_fill"),
    ),
    "engines/signal_hit": SIGNAL_HIT_SOURCES,
    # The same walk over 30 sessions with repair; it rewrites the 3 days
    # signal_hit just wrote, so it must not read the session's features earlier.
    "engines/signal_hit_fwd_fill": (
        *SIGNAL_HIT_SOURCES,
        ("features.stock_signal_lens_hit_daily", "engines/signal_hit"),
    ),
}

RESEARCH_AUX_ASSETS = [
    engines_opex,
    engines_vol_surface_svi,
    engines_iv_solver,
    engines_alert_scan,
    engines_signal_hit,
    engines_settlement,
    engines_terrain_intraday,
    engines_gex_intraday,
    engines_event_radar_sched,
    engines_macro_calendar,
    agents_morning_prep,
    agents_daily_digest,
    agents_weekly_policy_review,
    agents_eod_review,
    agents_journal_distill,
    maint_ensure_partitions,
    maint_vol_weekly_backfill,
    maint_terrain_backfill,
    maint_event_radar_purge,
    maint_signal_backfill,
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
        "research_iv_solver_schedule",
        "research_iv_solver_job",
        [engines_iv_solver],
        "25 23 * * 1-5",
        "UTC",
        "IV reconstructed",
    ),
    (
        "research_settlement_schedule",
        "research_settlement_job",
        [engines_settlement],
        # After the plugin's 1-hour bars land (~23:15 UTC): settlement reads the
        # settled session's hourly prints, and at 22:00 it had none to read.
        "45 23 * * 1-5",
        "UTC",
        "settlement",
    ),
    (
        "research_intraday_schedule",
        "research_intraday_job",
        [engines_terrain_intraday, engines_gex_intraday],
        # A quarter past the plugin's intraday chain (10:30 / 13:00 / 15:30 New
        # York): at :30 UTC the 10:30 run fired beside the chain it reads and found
        # no session OI, and the UTC clock drifted an hour off the chain each
        # winter. New York time keeps the two in step.
        "45 10-16 * * 1-5",
        "America/New_York",
        "intraday terrain+gex",
    ),
    (
        "research_event_radar_schedule",
        "research_event_radar_job",
        [engines_event_radar_sched],
        # Every day: the plugin writes Friday's filings early Saturday UTC.
        "*/30 * * * *",
        "UTC",
        "event-radar",
    ),
    # Weekly: the calendar changes a few times a year; the check alerts at most weekly.
    (
        "research_macro_calendar_schedule",
        "research_macro_calendar_job",
        [engines_macro_calendar],
        "0 10 * * 1",
        "UTC",
        "macro-calendar",
    ),
    # D2: the digest takes Morning Prep's slot; one post a day instead of one per hypothesis.
    (
        "research_daily_digest_schedule",
        "research_daily_digest_job",
        [agents_daily_digest],
        "30 11 * * 1-5",
        "UTC",
        "daily-digest",
    ),
    (
        "research_eod_review_schedule",
        "research_eod_review_job",
        [engines_candidate_outcome, agents_eod_review],
        "30 21 * * 1-5",
        "UTC",
        "eod-review",
    ),
    # D3: Sunday 22:00 UTC, before Monday's runs — the week's settled outcomes speak first.
    (
        "research_weekly_policy_review_schedule",
        "research_weekly_policy_review_job",
        [agents_weekly_policy_review],
        "0 22 * * 0",
        "UTC",
        "weekly-policy-review",
    ),
    # §20: distill after eod-review has settled and Flex fills have landed;
    # 23:55 UTC sits after settlement (23:45) on the same calendar.
    (
        "research_memory_distill_schedule",
        "research_memory_distill_job",
        [agents_journal_distill],
        "55 23 * * 1-5",
        "UTC",
        "journal-memory-distill",
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
    # keeps the dependency ordered on the same calendar. The session forecasts
    # the next trading day, which research_settlement_schedule (23:45 UTC)
    # settles that evening.
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
