"""Dagster assets that enqueue Plugin batch jobs via HTTP (no Plugin Python import).

Market / Flex workers remain the executors (ops_jobs.*). D10 BLOCKED.

Note: do not use ``from __future__ import annotations`` — Dagster validates
``context`` type hints at definition time and needs the live class object.
"""

import urllib.error

from dagster import AssetExecutionContext, AssetKey, MaterializeResult, asset

from bifrost_research.orchestration.flex_husbandry import (
    DEFAULT_MAX_AGE_HOURS,
    flex_ingest_verdict,
)
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
        "Catch-up enqueue for stock-eod + eod-pipeline (primary fire is "
        "market_universe_calendar @ 22:00 UTC / ~17:00 America/Chicago). "
        "POST /market/ingest/enqueue-slot → workers write raw_market.*"
    ),
)
def market_eod(context: AssetExecutionContext) -> MaterializeResult:
    return enqueue_market_slots(context, ("stock-eod", "eod-pipeline"))


@asset(
    key=AssetKey(["batch", "flex_trades"]),
    group_name="plugin_batch",
    description=(
        "Enqueue Flex trades via POST /flex/ingest/enqueue (fail if token source=none). "
        "Fired by research_flex_morning_schedule at 06:30 America/New_York Mon–Sat: IB "
        "generates the previous day's Activity statement overnight, so the 22:30 ET "
        "trading-day slot met [1003] every night. The plugin's worker waits for IB "
        "(deferred retries) — this asset only accepts the enqueue."
    ),
)
def flex_trades(context: AssetExecutionContext) -> MaterializeResult:
    return _enqueue_flex(context, slot="flex-trades")


@asset(
    key=AssetKey(["batch", "flex_transactions"]),
    group_name="plugin_batch",
    description=(
        "Enqueue Flex cash transactions via POST /flex/ingest/enqueue "
        "(research_flex_morning_schedule, 06:30 America/New_York Mon–Sat)."
    ),
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
        "Gate before Research dbt/engines. Market is judged on the doctor's "
        "EOD-critical verdict — what the session's tables actually hold "
        "(chain coverage, open interest, stock bars) — not on cron adherence, "
        "so a lagging rotate or a maintenance slot no longer blocks dbt. "
        "Flex is checked on its outcome (freshness-kpis: last attempt ok, last "
        "success within FLEX_GATE_MAX_AGE_HOURS) — an accepted enqueue is not a success."
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
    eod_verdict = "unknown"
    eod_detail = "doctor not probed"
    market_session = "unknown"
    flex_source = "unknown"
    try:
        doctor = get_json(f"{market_base}/market/doctor?probes=false", timeout=180.0)
        market_session = str(doctor.get("session") or "unknown")
        market_verdict = str(doctor.get("verdict") or "unknown")
        eod = doctor.get("eod_critical") if isinstance(doctor, dict) else None
        if isinstance(eod, dict):
            eod_verdict = str(eod.get("verdict") or "unknown")
            eod_detail = str(eod.get("detail") or "")
    except Exception as exc:  # noqa: BLE001
        context.log.warning("market doctor probe failed: %s", exc)

    try:
        summary = get_json(f"{flex_base}/flex/config/summary")
        flex_source = str(summary.get("source") or "unknown")
    except Exception as exc:  # noqa: BLE001
        context.log.warning("flex summary probe failed: %s", exc)

    flex_verdict, flex_reason = "unknown", "freshness-kpis not probed"
    try:
        kpis = get_json(f"{flex_base}/flex/dashboard/freshness-kpis")
        max_age = float(env("FLEX_GATE_MAX_AGE_HOURS", str(DEFAULT_MAX_AGE_HOURS)))
        flex_verdict, flex_reason = flex_ingest_verdict(kpis, max_age_hours=max_age)
    except Exception as exc:  # noqa: BLE001
        context.log.warning("flex freshness probe failed: %s", exc)

    if flex_source == "none":
        raise RuntimeError("husbandry_gate: Flex source=none — block dbt")
    if flex_verdict in ("failed", "stale"):
        raise RuntimeError(f"husbandry_gate: Flex ingest {flex_verdict} ({flex_reason}) — block dbt")
    if eod_verdict == "critical":
        raise RuntimeError(
            f"husbandry_gate: Market EOD session {market_session} incomplete — {eod_detail}"
        )

    context.log.info(
        "husbandry_gate ok market=%s eod=%s session=%s flex_source=%s flex_ingest=%s (%s)",
        market_verdict,
        eod_verdict,
        market_session,
        flex_source,
        flex_verdict,
        flex_reason,
    )
    return MaterializeResult(
        metadata=meta(
            {
                "market_verdict": market_verdict,
                "market_eod": eod_verdict,
                "market_eod_detail": eod_detail,
                "market_session": market_session,
                "flex_source": flex_source,
                "flex_ingest": flex_verdict,
                "flex_ingest_reason": flex_reason,
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
