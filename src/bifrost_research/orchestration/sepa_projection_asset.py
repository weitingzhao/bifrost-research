"""Dagster asset: dw_stock.mart_sepa_feature_daily → features.stock_signal_sepa_daily."""

from typing import Any

from dagster import AssetExecutionContext, AssetKey, MaterializeResult, asset

from bifrost_research.orchestration.sepa_projection import run_sepa_projection

# Soft upstream: husbandry_gate (market_eod + flex enqueues) must pass first.
_GATE = AssetKey(["batch", "husbandry_gate"])


def _metadata(result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
        elif value is None:
            continue
        else:
            out[key] = str(value)[:500]
    out["advisory"] = "D10 BLOCKED"
    return out


@asset(
    key=AssetKey(["features", "sepa_projection"]),
    deps=[_GATE],
    group_name="feature_store",
    description=(
        "Project dbt mart_sepa_feature_daily → features.stock_signal_sepa_daily. "
        "Runs after husbandry_gate (Market EOD + Flex enqueue). Prefer materializing "
        "dbt assets in the same job before this asset when manifest is present."
    ),
)
def sepa_projection(context: AssetExecutionContext) -> MaterializeResult:
    from bifrost_research.db.conn import connect

    conn = connect()
    try:
        result = run_sepa_projection(conn)
        context.log.info("sepa_projection result=%s", result)
        if result.get("skipped") and result.get("reason"):
            context.log.warning("sepa_projection skipped: %s", result.get("reason"))
        return MaterializeResult(metadata=_metadata(result))
    finally:
        try:
            conn.close()
        except Exception:
            pass


SEPA_PROJECTION_ASSETS = [sepa_projection]
