"""dagster-dbt integration for src/bifrost_research/dbt/.

When ``target/manifest.json`` is missing (fresh clone / CI without ``dbt parse``),
no dbt assets are registered — Definitions still load with engine assets only.

Note: avoid ``from __future__ import annotations`` — Dagster needs live context types.
"""

from typing import Any, List

from dagster import AssetExecutionContext
from dagster_dbt import DbtCliResource, dbt_assets

from bifrost_research.orchestration.paths import (
    DBT_MANIFEST_PATH,
    DBT_PROFILES_DIR,
    DBT_PROJECT_DIR,
    dbt_manifest_exists,
)


def build_dbt_resource() -> DbtCliResource:
    return DbtCliResource(
        project_dir=str(DBT_PROJECT_DIR),
        profiles_dir=str(DBT_PROFILES_DIR),
    )


def load_dbt_assets() -> List[Any]:
    """Return dagster-dbt assets when a compiled manifest is present."""
    if not dbt_manifest_exists():
        return []

    @dbt_assets(manifest=DBT_MANIFEST_PATH)
    def bifrost_research_dbt_assets(
        context: AssetExecutionContext,
        dbt: DbtCliResource,
    ):
        """SEPA dbt project — staging / intermediate / marts → dw_stock.*."""
        yield from dbt.cli(["build"], context=context).stream()

    return [bifrost_research_dbt_assets]
