"""dagster-dbt integration for src/bifrost_research/dbt/.

When ``target/manifest.json`` is missing (fresh clone / CI without ``dbt parse``),
no dbt assets are registered — Definitions still load with engine assets only.

Note: avoid ``from __future__ import annotations`` — Dagster needs live context types.
"""

from typing import Any, List, Mapping, Optional

from dagster import AssetExecutionContext, AssetKey, AssetSpec
from dagster_dbt import DagsterDbtTranslator, DbtCliResource, DbtProject, dbt_assets

from bifrost_research.orchestration.engine_assets import canonical_pnl, gex, volatility
from bifrost_research.orchestration.paths import (
    DBT_MANIFEST_PATH,
    DBT_PROFILES_DIR,
    DBT_PROJECT_DIR,
    dbt_manifest_exists,
)
from bifrost_research.orchestration.plugin_batch_assets import (
    FLEX_DERIVED_SCHEMAS,
    flex_gate,
    husbandry_gate,
)

# dbt nodes that become assets; sources are external keys, tests are checks.
_GATED_RESOURCE_TYPES = frozenset({"model", "seed", "snapshot"})

# dbt sources that a Research engine writes, keyed (source_name, table), mapped to
# the asset that writes them. A model reading one waits for that engine as well as
# the gate. On 2026-09-29 mart_sepa_tier_options was built at 02:31 from Friday's IV
# percentile, PCR and GEX levels, because volatility and gex wrote Monday's rows at
# 02:33-02:36. test_every_dbt_source_is_ingested_or_has_a_writer keeps it complete.
ENGINE_WRITTEN_SOURCES: Mapping[tuple[str, str], AssetKey] = {
    ("features", "option_metric_iv_percentile_daily"): volatility.key,
    ("features", "option_metric_pcr_daily"): volatility.key,
    ("features", "option_metric_gex_levels_daily"): gex.key,
    ("features", "stock_signal_canonical_pnl_daily"): canonical_pnl.key,
}


def _engine_writers(
    manifest: Mapping[str, Any], resource_props: Mapping[str, Any]
) -> set[AssetKey]:
    """Engines that write a source the node reads, and flex_gate for a Flex-derived one."""
    sources = manifest.get("sources", {})
    writers = set()
    for node_id in resource_props.get("depends_on", {}).get("nodes", []):
        source = sources.get(node_id)
        if source is not None:
            writer = ENGINE_WRITTEN_SOURCES.get((source["source_name"], source["name"]))
            if writer is not None:
                writers.add(writer)
            if source.get("schema") in FLEX_DERIVED_SCHEMAS:
                writers.add(flex_gate.key)
    return writers


class GatedDbtTranslator(DagsterDbtTranslator):
    """Every dbt asset waits for ``batch/husbandry_gate`` (Owner decision 2026-09-29),
    and a model that reads an engine's table also waits for that engine. A model
    reading a Flex-derived source (``FLEX_DERIVED_SCHEMAS``) also waits for
    ``batch/flex_gate``; none does, so a Flex failure no longer stops dbt (TD-192).

    The default translator derives deps from the manifest alone, so the dbt
    assets' only upstreams were their dbt sources (``market.short_volume``, …)
    and nothing tied them to the gate. On 2026-09-29 dbt started 16s before the
    gate and ran beside it; a failing gate would have blocked the engines and
    left dbt building on an incomplete session. Adding the gate as a dep puts an
    edge in the graph: when it fails, the dbt step never starts. The engine deps
    work the same way, and since dbt is one step, the whole build waits for them.
    """

    def get_asset_spec(
        self,
        manifest: Mapping[str, Any],
        unique_id: str,
        project: Optional[DbtProject],
    ) -> AssetSpec:
        spec = super().get_asset_spec(manifest, unique_id, project)
        resource_props = self.get_resource_props(manifest, unique_id)
        if resource_props.get("resource_type") not in _GATED_RESOURCE_TYPES:
            return spec
        upstream = {husbandry_gate.key, *_engine_writers(manifest, resource_props)}
        missing = upstream - {dep.asset_key for dep in spec.deps}
        if not missing:
            return spec
        return spec.merge_attributes(deps=sorted(missing, key=AssetKey.to_user_string))


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
