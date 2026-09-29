"""dagster-dbt integration for src/bifrost_research/dbt/.

When ``target/manifest.json`` is missing (fresh clone / CI without ``dbt parse``),
no dbt assets are registered — Definitions still load with engine assets only.

Note: avoid ``from __future__ import annotations`` — Dagster needs live context types.
"""

from typing import Any, List, Mapping, Optional

from dagster import AssetExecutionContext, AssetSpec
from dagster_dbt import DagsterDbtTranslator, DbtCliResource, DbtProject, dbt_assets

from bifrost_research.orchestration.paths import (
    DBT_MANIFEST_PATH,
    DBT_PROFILES_DIR,
    DBT_PROJECT_DIR,
    dbt_manifest_exists,
)
from bifrost_research.orchestration.plugin_batch_assets import husbandry_gate

# dbt nodes that become assets; sources are external keys, tests are checks.
_GATED_RESOURCE_TYPES = frozenset({"model", "seed", "snapshot"})


class GatedDbtTranslator(DagsterDbtTranslator):
    """Every dbt asset waits for ``batch/husbandry_gate`` (Owner decision 2026-09-29).

    The default translator derives deps from the manifest alone, so the dbt
    assets' only upstreams were their dbt sources (``market.short_volume``, …)
    and nothing tied them to the gate. On 2026-09-29 dbt started 16s before the
    gate and ran beside it; a failing gate would have blocked the engines and
    left dbt building on an incomplete session. Adding the gate as a dep puts an
    edge in the graph: when it fails, the dbt step never starts.
    """

    def get_asset_spec(
        self,
        manifest: Mapping[str, Any],
        unique_id: str,
        project: Optional[DbtProject],
    ) -> AssetSpec:
        spec = super().get_asset_spec(manifest, unique_id, project)
        resource_type = self.get_resource_props(manifest, unique_id).get("resource_type")
        if resource_type not in _GATED_RESOURCE_TYPES:
            return spec
        if any(dep.asset_key == husbandry_gate.key for dep in spec.deps):
            return spec
        return spec.merge_attributes(deps=[husbandry_gate.key])


def build_dbt_resource() -> DbtCliResource:
    return DbtCliResource(
        project_dir=str(DBT_PROJECT_DIR),
        profiles_dir=str(DBT_PROFILES_DIR),
    )


def load_dbt_assets() -> List[Any]:
    """Return dagster-dbt assets when a compiled manifest is present."""
    if not dbt_manifest_exists():
        return []

    @dbt_assets(manifest=DBT_MANIFEST_PATH, dagster_dbt_translator=GatedDbtTranslator())
    def bifrost_research_dbt_assets(
        context: AssetExecutionContext,
        dbt: DbtCliResource,
    ):
        """SEPA dbt project — staging / intermediate / marts → dw_stock.*."""
        yield from dbt.cli(["build"], context=context).stream()

    return [bifrost_research_dbt_assets]
