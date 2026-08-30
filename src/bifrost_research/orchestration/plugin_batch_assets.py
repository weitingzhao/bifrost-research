"""Dagster assets that enqueue Plugin batch jobs via HTTP (no Plugin Python import).

Market / Flex workers remain the executors (ops_jobs.*). D10 BLOCKED.

Note: do not use ``from __future__ import annotations`` — Dagster validates
``context`` type hints at definition time and needs the live class object.
"""

import urllib.error
from typing import Any

from dagster import AssetExecutionContext, AssetKey, MaterializeResult, asset

from bifrost_research.orchestration.plugin_http import (
    enqueue_market_slots,
    env,
    get_json,
    meta,
    post_json,
)


@asset(
    key=AssetKey(["batch", "market_eod"]),
    group_name="plugin_batch",
    description=(
        "Enqueue Market EOD slots (stock-eod + eod-pipeline) via Plugin "
        "POST /market/ingest/enqueue-slot. Workers write raw_market.*"
    ),
)
def market_eod(context: AssetExecutionContext) -> MaterializeResult:
    return enqueue_market_slots(context, ("stock-eod", "eod-pipeline"))


@asset(
    key=AssetKey(["batch", "flex_trades"]),
    group_name="plugin_batch",
    description="Enqueue Flex trades day-end via POST /flex/ingest/enqueue (fail if token source=none).",
)
def flex_trades(context: AssetExecutionContext) -> MaterializeResult:
    return _enqueue_flex(context, slot="flex-trades")


@asset(
    key=AssetKey(["batch", "flex_transactions"]),
    group_name="plugin_batch",
    description="Enqueue Flex cash transactions via POST /flex/ingest/enqueue.",
)
def flex_transactions(context: AssetExecutionContext) -> MaterializeResult:
    return _enqueue_flex(context, slot="flex-transactions")


def _enqueue_flex(context: AssetExecutionContext, *, slot: str) -> MaterializeResult:
    base = env(
        "FLEX_QUERY_API_URL",
        "http://flex-query-api.plugin-flex-query.svc.cluster.local:8791",
    ).rstrip("/")
    token = env("FLEX_QUERY_WRITE_TOKEN") or env("MARKET_DATA_WRITE_TOKEN")
    if not token:
        raise RuntimeError("FLEX_QUERY_WRITE_TOKEN (or MARKET_DATA_WRITE_TOKEN) required")

    try:
        summary = get_json(f"{base}/flex/config/summary")
        source = str(summary.get("source") or "")
        if source == "none":
            raise RuntimeError(
                "Flex token source=none — refuse enqueue (husbandry fail-closed)"
            )
        context.log.info("flex config source=%s", source)
    except urllib.error.URLError as exc:
        context.log.warning("flex config/summary unreachable: %s — continuing enqueue", exc)

    url = f"{base}/flex/ingest/enqueue"
    context.log.info("enqueue flex slot=%s → %s", slot, url)
    result = post_json(
        url,
        {"slot": slot},
        token_header="X-Flex-Query-Write-Token",
        token=token,
    )
    context.log.info("flex slot=%s result=%s", slot, result)
    if isinstance(result, dict) and result.get("ok") is False:
        raise RuntimeError(f"flex enqueue failed: {result}")
    out = meta(result if isinstance(result, dict) else {"raw": str(result)})
    out["slot"] = slot
    return MaterializeResult(metadata=out)


@asset(
    key=AssetKey(["batch", "husbandry_gate"]),
    deps=[
        AssetKey(["batch", "market_eod"]),
        AssetKey(["batch", "flex_trades"]),
        AssetKey(["batch", "flex_transactions"]),
    ],
    group_name="plugin_batch",
    description=(
        "Gate before Research dbt/engines: Market husbandry.verdict and Flex "
        "token/freshness must not be degraded. Blocks dual-track when red. "
        "draining is allowed (queue may still be processing non-EOD slots)."
    ),
)
def husbandry_gate(context: AssetExecutionContext) -> MaterializeResult:
    market_base = env(
        "MARKET_DATA_API_URL",
        "http://market-data-api.plugin-market-data.svc.cluster.local:8790",
    ).rstrip("/")
    flex_base = env(
        "FLEX_QUERY_API_URL",
        "http://flex-query-api.plugin-flex-query.svc.cluster.local:8791",
    ).rstrip("/")

    market_verdict = "unknown"
    flex_source = "unknown"
    try:
        dash = get_json(f"{market_base}/market/ingest/queue-dashboard")
        hus = dash.get("husbandry") if isinstance(dash, dict) else None
        if isinstance(hus, dict):
            market_verdict = str(hus.get("verdict") or "unknown")
    except Exception as exc:  # noqa: BLE001
        context.log.warning("market husbandry probe failed: %s", exc)

    try:
        summary = get_json(f"{flex_base}/flex/config/summary")
        flex_source = str(summary.get("source") or "unknown")
    except Exception as exc:  # noqa: BLE001
        context.log.warning("flex summary probe failed: %s", exc)

    if flex_source == "none":
        raise RuntimeError("husbandry_gate: Flex source=none — block dbt")
    if market_verdict in ("missed", "degraded"):
        raise RuntimeError(
            f"husbandry_gate: Market verdict={market_verdict} — block dbt"
        )

    context.log.info(
        "husbandry_gate ok market=%s flex_source=%s", market_verdict, flex_source
    )
    return MaterializeResult(
        metadata=meta(
            {
                "market_verdict": market_verdict,
                "flex_source": flex_source,
                "gate": "pass",
            }
        )
    )


PLUGIN_BATCH_ASSETS = [
    market_eod,
    flex_trades,
    flex_transactions,
    husbandry_gate,
]
