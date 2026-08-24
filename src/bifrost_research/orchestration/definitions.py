"""Dagster Definitions entrypoint for Bifrost Research Engine.

Pipeline (Wave 5.1)::

    Plugin market ingest (external)
      → dbt transforms (analytics.*)          [when target/manifest.json exists]
      → Python analytics engines
      → AI forecast / event radar / backtest

Load::

    from bifrost_research.orchestration.definitions import defs

Or::

    dagster dev -m bifrost_research.orchestration.definitions

D10 BLOCKED — no trade execution paths.
"""

from __future__ import annotations

from dagster import Definitions

from bifrost_research.orchestration.dbt_assets import build_dbt_resource, load_dbt_assets
from bifrost_research.orchestration.engine_assets import ENGINE_ASSETS, plugin_market_ingest


def build_definitions() -> Definitions:
    """Construct Definitions (dbt optional when manifest missing)."""
    dbt_asset_defs = load_dbt_assets()
    resources = {}
    if dbt_asset_defs:
        resources["dbt"] = build_dbt_resource()
    return Definitions(
        assets=[plugin_market_ingest, *dbt_asset_defs, *ENGINE_ASSETS],
        resources=resources,
    )


defs = build_definitions()
