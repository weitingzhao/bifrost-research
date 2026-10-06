"""Smoke tests for Dagster Definitions (Data Husbandry + Wave 5.1).

Skipped when ``dagster`` is not installed (optional ``[orchestration]`` extra).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("dagster")
pytest.importorskip("dagster_dbt")

from dagster import AssetKey, Definitions


def test_definitions_load_without_dbt_manifest() -> None:
    """Definitions must load even when dbt target/manifest.json is absent."""
    from bifrost_research.orchestration.dbt_assets import load_dbt_assets
    from bifrost_research.orchestration.definitions import build_definitions

    with patch(
        "bifrost_research.orchestration.dbt_assets.dbt_manifest_exists",
        return_value=False,
    ):
        assert load_dbt_assets() == []
        defs = build_definitions()

    assert isinstance(defs, Definitions)
    keys = {k.to_user_string() for k in defs.resolve_all_asset_keys()}
    assert "external/plugin_market_ingest" in keys
    assert "batch/market_eod" in keys
    assert "batch/flex_trades" in keys
    assert "batch/husbandry_gate" in keys
    assert "features/sepa_projection" in keys
    for engine in (
        "volatility",
        "momentum",
        "gex",
        "surface",
        "flow",
        "terrain",
        "forecast",
        "event_radar",
        "backtest",
        "vrp",
        "vrp_fwd_ret_20d",
    ):
        assert f"engines/{engine}" in keys


def test_definitions_include_schedule() -> None:
    from bifrost_research.orchestration.definitions import build_definitions

    with patch(
        "bifrost_research.orchestration.dbt_assets.dbt_manifest_exists",
        return_value=False,
    ):
        defs = build_definitions()
    names = {s.name for s in defs.schedules}
    assert "research_trading_day_schedule" in names
    assert "research_flex_morning_schedule" in names
    job_names = {j.name for j in defs.jobs}
    assert "research_trading_day" in job_names
    assert "research_flex_morning" in job_names


def test_flex_is_enqueued_in_the_morning_not_at_close() -> None:
    """22:30 ET is before IB generates the statement; Flex moved to 06:30 ET Mon–Sat."""
    from bifrost_research.orchestration.schedules import (
        research_flex_morning_job,
        research_flex_morning_schedule,
        research_trading_day_job,
    )

    assert research_flex_morning_schedule.cron_schedule == "30 6 * * 1-6"
    assert research_flex_morning_schedule.execution_timezone == "America/New_York"

    with patch(
        "bifrost_research.orchestration.dbt_assets.dbt_manifest_exists",
        return_value=False,
    ):
        from bifrost_research.orchestration.definitions import build_definitions

        defs = build_definitions()
    graph = defs.resolve_asset_graph()
    flex = {AssetKey(["batch", "flex_trades"]), AssetKey(["batch", "flex_transactions"])}
    morning = research_flex_morning_job.selection.resolve(graph)
    assert morning == flex
    trading_day = research_trading_day_job.selection.resolve(graph)
    assert not (trading_day & flex)
    assert AssetKey(["batch", "husbandry_gate"]) in trading_day


def test_definitions_include_dbt_when_manifest_present() -> None:
    """When a manifest path exists, dagster-dbt assets are registered."""
    from bifrost_research.orchestration.paths import DBT_MANIFEST_PATH

    if not DBT_MANIFEST_PATH.is_file():
        pytest.skip("dbt target/manifest.json not present — run dbt parse/compile first")

    from bifrost_research.orchestration.definitions import build_definitions

    defs = build_definitions()
    assert isinstance(defs, Definitions)
    keys = defs.resolve_all_asset_keys()
    # At least one dbt model key should appear (staging / marts).
    assert any("stg_" in k.to_user_string() or "mart_" in k.to_user_string() for k in keys)


def test_module_entrypoint_defs() -> None:
    from bifrost_research.orchestration.definitions import defs

    assert isinstance(defs, Definitions)
    # Forecast depends on terrain in the asset graph
    graph = defs.resolve_asset_graph()
    forecast_key = AssetKey(["engines", "forecast"])
    parents = graph.get(forecast_key).parent_keys
    assert AssetKey(["engines", "terrain"]) in parents
    # Volatility gated on husbandry batch
    vol_key = AssetKey(["engines", "volatility"])
    vol_parents = graph.get(vol_key).parent_keys
    assert AssetKey(["batch", "market_eod"]) in vol_parents
    assert AssetKey(["batch", "husbandry_gate"]) in vol_parents
    # A4: VRP runs after volatility (the day's ATM IV must exist), fwd_ret after VRP,
    # and the 23:10 UTC aux schedule that wrote IV-less rows is gone.
    assert vol_key in graph.get(AssetKey(["engines", "vrp"])).parent_keys
    assert AssetKey(["engines", "vrp"]) in graph.get(AssetKey(["engines", "vrp_fwd_ret_20d"])).parent_keys
    assert "research_vrp_schedule" not in {s.name for s in defs.schedules}


def test_husbandry_whitelist_is_exactly_the_declared_schedule_set() -> None:
    """The API's schedule whitelist must equal Dagster's declared schedules.

    On 2026-09-11 two drifts cancelled out: a retired ``research_vrp_schedule``
    still listed, and ``market_ticker_details_schedule`` never registered. The
    totals matched (35 vs 35), so nothing looked wrong while ``/signal-health``
    attributed stale ``vrp`` to a schedule that does not exist, sending the
    operator to inspect nothing.
    """
    from bifrost_research.api.orchestration_schedules import HUSBANDRY_SCHEDULE_JOBS
    from bifrost_research.orchestration.definitions import defs

    declared = {s.name for s in defs.schedules}
    listed = {name for name, _job, _tz in HUSBANDRY_SCHEDULE_JOBS}
    assert listed == declared, (
        f"ghosts (listed, not declared)={sorted(listed - declared)} "
        f"missing (declared, not listed)={sorted(declared - listed)}"
    )


def test_husbandry_tz_matches_schedule_definitions() -> None:
    """Whitelist tz must equal each ScheduleDefinition.execution_timezone.

    The instigator row has cron but not tz; next_tick_at used to treat every
    cron as UTC. Sources: schedules.py:85,108 (NY); market_slot_schedules.py:65
    vs :286 (UTC vs NY intraday); research_aux_schedules.py:310 (per spec).
    """
    from bifrost_research.api.orchestration_schedules import HUSBANDRY_SCHEDULE_JOBS
    from bifrost_research.orchestration.definitions import defs

    declared = {s.name: (s.execution_timezone or "UTC") for s in defs.schedules}
    for name, _job, tz in HUSBANDRY_SCHEDULE_JOBS:
        assert declared[name] == tz, f"{name}: whitelist {tz!r} vs definition {declared[name]!r}"


def test_trading_day_caps_step_concurrency() -> None:
    """Uncapped, the gate released ~7 steps into one 2Gi container and it OOMKilled.

    2026-09-11 02:34:31: dbt + five engines + option_universe started together,
    the dagster-daemon container died, and the run with it -- mid-dbt, after the
    CASCADE that drops mart_sepa_criteria_stats and before anything rebuilt it.
    """
    from bifrost_research.orchestration.definitions import defs
    from bifrost_research.orchestration.schedules import TRADING_DAY_MAX_CONCURRENT

    job = defs.resolve_job_def("research_trading_day")
    execution = job.run_config["execution"]["config"]["multiprocess"]
    assert execution["max_concurrent"] == TRADING_DAY_MAX_CONCURRENT
    assert 1 <= TRADING_DAY_MAX_CONCURRENT <= 3


def test_market_schedules_cover_the_subscribed_slots_only() -> None:
    """option-trades left the schedule (Options Starter); ratios/short data joined it."""
    from bifrost_research.orchestration.market_slot_schedules import (
        ENQUEUE_RETRY,
        MARKET_SCHEDULES,
        market_corporate,
        market_fundamentals_market,
    )

    names = {s.name for s in MARKET_SCHEDULES}
    assert "market_corporate_schedule" in names
    assert "market_fundamentals_market_schedule" in names
    assert "market_corporate_trades_schedule" not in names
    by_name = {s.name: s for s in MARKET_SCHEDULES}
    assert by_name["market_fundamentals_market_schedule"].cron_schedule == "30 4 * * 2-6"
    assert ENQUEUE_RETRY.max_retries == 3
    assert market_corporate.op.retry_policy is ENQUEUE_RETRY
    assert market_fundamentals_market.op.retry_policy is ENQUEUE_RETRY


def test_the_ratios_schedule_polls_daily_outside_the_collection_window() -> None:
    """Ratios arrive the morning after a session; the 04:30 Tue–Sat run asks too early."""
    from bifrost_research.orchestration.market_slot_schedules import (
        ENQUEUE_RETRY,
        MARKET_SCHEDULES,
        market_ratios_market,
    )

    sched = {s.name: s for s in MARKET_SCHEDULES}["market_ratios_market_schedule"]
    assert sched.cron_schedule == "10 5-8,11,14,20 * * *"
    assert sched.execution_timezone == "UTC"
    assert market_ratios_market.op.retry_policy is ENQUEUE_RETRY
    minute, hour_field, dom, month, dow = sched.cron_schedule.split()
    assert (dom, month, dow) == ("*", "*", "*"), "must run every day, weekends included"
    hours: set[int] = set()
    for part in hour_field.split(","):
        lo, _, hi = part.partition("-")
        hours.update(range(int(lo), int(hi or lo) + 1))
    assert len(hours) <= 7
    assert not any(21 <= h <= 23 for h in hours), "must not land in the 21:05-23:15 UTC collection window"
    assert 4 not in hours, "must not collide with the 04:30 fundamentals-market run"
    assert minute == "10"


def test_the_option_depth_schedule_is_weekly_on_sunday_morning() -> None:
    """New universe names get their option history without an Owner firing a one-off."""
    from bifrost_research.orchestration.market_slot_schedules import (
        ENQUEUE_RETRY,
        MARKET_SCHEDULES,
        market_option_depth,
    )

    sched = {s.name: s for s in MARKET_SCHEDULES}["market_option_depth_schedule"]
    assert sched.cron_schedule == "30 7 * * 0"
    assert sched.execution_timezone == "UTC"
    assert market_option_depth.op.retry_policy is ENQUEUE_RETRY


def test_the_corporate_backfill_schedule_is_monthly_and_out_of_the_collection_window() -> None:
    """History is a monthly errand; the -7/+60 day window is the nightly one."""
    from bifrost_research.orchestration.market_slot_schedules import MARKET_SCHEDULES

    sched = {s.name: s for s in MARKET_SCHEDULES}["market_corporate_backfill_schedule"]
    assert sched.cron_schedule == "0 7 1 * *"
    assert sched.execution_timezone == "UTC"
    hour = sched.cron_schedule.split()[1]
    assert not (21 <= int(hour) <= 23), "must not land in the 21:05-23:15 UTC collection window"


def test_definitions_include_the_failure_sensor() -> None:
    from bifrost_research.orchestration.definitions import build_definitions

    with patch(
        "bifrost_research.orchestration.dbt_assets.dbt_manifest_exists",
        return_value=False,
    ):
        defs = build_definitions()
    assert "bifrost_run_failure_alert" in {s.name for s in defs.sensors}


def test_every_whitelisted_job_resolves_its_assets() -> None:
    """A schedule can be declared while its asset never joins the assets list.

    2026-09-27: research_memory_distill_schedule shipped with its asset
    missing from RESEARCH_AUX_ASSETS — the whitelist test passed (the
    schedule was declared) and the daemon then failed every tick with
    DagsterInvalidSubsetError. Resolving each whitelisted job here is the
    same resolution the daemon performs.
    """
    from bifrost_research.api.orchestration_schedules import HUSBANDRY_SCHEDULE_JOBS
    from bifrost_research.orchestration.definitions import defs

    for _sched, job_name, _tz in HUSBANDRY_SCHEDULE_JOBS:
        defs.get_job_def(job_name)  # raises DagsterInvalidSubsetError on drift


def test_schedule_roster_matches_the_definitions_field_by_field() -> None:
    """research-api cannot import Dagster, so it reads schedule_roster (TD-108).

    Every field it serves -- job, timezone, cron, the plugin slots a schedule
    fires -- must equal the code's, or Console, /metrics and the liveness alerts
    describe a schedule that is not the one running.
    """
    from bifrost_research.api.schedule_roster import SCHEDULE_ROSTER
    from bifrost_research.orchestration.definitions import defs
    from bifrost_research.orchestration.market_slot_schedules import MARKET_SLOTS_BY_SCHEDULE

    declared = {s.name: s for s in defs.schedules}
    assert len({s.name for s in SCHEDULE_ROSTER}) == len(SCHEDULE_ROSTER), "duplicate names"
    for spec in SCHEDULE_ROSTER:
        sched = declared[spec.name]
        assert spec.job == sched.job_name, spec.name
        assert spec.tz == (sched.execution_timezone or "UTC"), spec.name
        assert spec.cron == sched.cron_schedule, spec.name
        assert spec.market_slots == MARKET_SLOTS_BY_SCHEDULE.get(spec.name, ()), spec.name


def test_schedule_names_in_docs_and_scripts_exist() -> None:
    """A README table and a runbook script kept their own schedule lists and drifted.

    k8s/orchestration/README listed research_morning_prep_schedule; the verify
    script asserted market_corporate_trades_schedule and only warned when it was
    absent. Any *_schedule named in those places must be a declared schedule.
    """
    import re
    from pathlib import Path

    from bifrost_research.api.schedule_roster import ROSTER_BY_NAME

    root = Path(__file__).resolve().parents[2]
    files = [*root.glob("k8s/**/*.md"), *root.glob("scripts/*.sh"), root / "Makefile"]
    pattern = re.compile(r"\b((?:research|market)_[a-z0-9_]+_schedule)\b")
    unknown = {
        f"{path.relative_to(root)}: {name}"
        for path in files
        for name in pattern.findall(path.read_text(encoding="utf-8"))
        if name not in ROSTER_BY_NAME
    }
    assert not unknown, sorted(unknown)
