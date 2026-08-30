"""Dagster Definitions entrypoint for Bifrost Research Engine.

Pipeline (Data Husbandry + Dagster)::

    batch market_eod / flex_* (HTTP enqueue → Plugin workers)
      → husbandry_gate
      → dbt transforms (dw_stock.*)          [when target/manifest.json exists]
      → sepa_projection → features.stock_signal_sepa_daily
      → Python analytics engines → scan

    Plus multi-schedule market_* UTC slots and research_* aux schedules
    (former CronJobs). D10 BLOCKED.

Load::

    from bifrost_research.orchestration.definitions import defs
"""

from __future__ import annotations

from dagster import Definitions

from bifrost_research.orchestration.dbt_assets import build_dbt_resource, load_dbt_assets
from bifrost_research.orchestration.engine_assets import ENGINE_ASSETS, plugin_market_ingest
from bifrost_research.orchestration.market_slot_schedules import MARKET_SCHEDULE_ASSETS
from bifrost_research.orchestration.plugin_batch_assets import PLUGIN_BATCH_ASSETS
from bifrost_research.orchestration.research_aux_schedules import RESEARCH_AUX_ASSETS
from bifrost_research.orchestration.schedules import RESEARCH_JOBS, RESEARCH_SCHEDULES
from bifrost_research.orchestration.sepa_projection_asset import SEPA_PROJECTION_ASSETS


def build_definitions() -> Definitions:
    """Construct Definitions (dbt optional when manifest missing)."""
    dbt_asset_defs = load_dbt_assets()
    resources = {}
    if dbt_asset_defs:
        resources["dbt"] = build_dbt_resource()
    return Definitions(
        assets=[
            plugin_market_ingest,
            *PLUGIN_BATCH_ASSETS,
            *MARKET_SCHEDULE_ASSETS,
            *dbt_asset_defs,
            *SEPA_PROJECTION_ASSETS,
            *ENGINE_ASSETS,
            *RESEARCH_AUX_ASSETS,
        ],
        jobs=RESEARCH_JOBS,
        schedules=RESEARCH_SCHEDULES,
        resources=resources,
    )


defs = build_definitions()
