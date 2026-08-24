"""Smoke tests for Dagster Definitions (Wave 5.1).

Skipped when ``dagster`` is not installed (optional ``[orchestration]`` extra).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("dagster")
pytest.importorskip("dagster_dbt")

from dagster import AssetKey, Definitions  # noqa: E402


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
