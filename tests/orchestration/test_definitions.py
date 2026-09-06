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


def test_definitions_include_the_failure_sensor() -> None:
    from bifrost_research.orchestration.definitions import build_definitions

    with patch(
        "bifrost_research.orchestration.dbt_assets.dbt_manifest_exists",
        return_value=False,
    ):
        defs = build_definitions()
    assert "bifrost_run_failure_alert" in {s.name for s in defs.sensors}
