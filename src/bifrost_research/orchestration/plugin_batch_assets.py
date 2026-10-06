"""Dagster assets that enqueue Plugin batch jobs via HTTP (no Plugin Python import).

Market / Flex workers remain the executors (ops_jobs.*). D10 BLOCKED.

Note: do not use ``from __future__ import annotations`` — Dagster validates
``context`` type hints at definition time and needs the live class object.
"""

import logging
import time
import urllib.error
from datetime import date
from typing import Any, Callable

from dagster import AssetExecutionContext, AssetKey, Config, MaterializeResult, asset

from bifrost_research.orchestration.flex_husbandry import (
    DEFAULT_MAX_AGE_HOURS,
    flex_ingest_verdict,
)
from bifrost_research.orchestration.plugin_http import (
    enqueue_market_slots,
    env,
    get_json,
    MARKET_DOCTOR_TIMEOUT_SEC,
    market_doctor_url,
    meta,
    post_json,
)

logger = logging.getLogger(__name__)


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


class HusbandryGateConfig(Config):
    """Overrides for a gate launched by hand; the schedule always runs the defaults.

    ``allow_unknown`` lets a probe that did not answer pass (the gate fails closed
    otherwise). ``expected_session`` (YYYY-MM-DD) replaces the latest closed New
    York session the doctor must have judged — for re-running an older session.
    """

    allow_unknown: bool = False
    expected_session: str = ""


#: Seconds before the one retry of a probe that raised (not after a timeout:
#: the doctor already had MARKET_DOCTOR_TIMEOUT_SEC).
PROBE_RETRY_SEC = 15.0


def expected_session() -> date:
    """The session tonight's batch closes: the newest NYSE session whose close has passed."""
    from bifrost_research.db.calendar import latest_closed_session
    from bifrost_research.db.conn import connect

    conn = connect()
    try:
        return latest_closed_session(conn)
    finally:
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("closing the calendar connection failed: %s", exc)


def _probe(context: AssetExecutionContext, label: str, fn: Callable[[], Any]) -> tuple[Any, str | None]:
    """(answer, None) or (None, error) after one retry; a timeout is not retried."""
    for attempt in (1, 2):
        try:
            return fn(), None
        except Exception as exc:  # noqa: BLE001 — the caller fails closed on the error
            error = f"{type(exc).__name__}: {exc}"
            timed_out = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            context.log.warning("%s probe failed (attempt %d): %s", label, attempt, error)
            if attempt == 2 or timed_out:
                return None, error
            time.sleep(PROBE_RETRY_SEC)
    return None, "unreachable"


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
        "so a lagging rotate or a maintenance slot no longer blocks dbt. The "
        "doctor must have judged tonight's New York session. "
        "Flex is checked on its outcome (freshness-kpis: last attempt ok, last "
        "success within FLEX_GATE_MAX_AGE_HOURS) — an accepted enqueue is not a success. "
        "Fails closed: a probe that does not answer blocks (TD-94)."
    ),
)
def husbandry_gate(context: AssetExecutionContext, config: HusbandryGateConfig) -> MaterializeResult:
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
    market_generated_at = "unknown"
    flex_source = "unknown"
    unknown: list[str] = []

    doctor, error = _probe(
        context,
        "market doctor",
        lambda: get_json(
            market_doctor_url(market_base, probes=False), timeout=MARKET_DOCTOR_TIMEOUT_SEC
        ),
    )
    if isinstance(doctor, dict):
        market_session = str(doctor.get("session") or "unknown")
        market_generated_at = str(doctor.get("generated_at") or "unknown")
        market_verdict = str(doctor.get("verdict") or "unknown")
        eod = doctor.get("eod_critical")
        if isinstance(eod, dict):
            eod_verdict = str(eod.get("verdict") or "unknown")
            eod_detail = str(eod.get("detail") or "")
    else:
        eod_detail = f"doctor probe failed: {error or 'not a JSON object'}"
    if eod_verdict == "unknown":
        unknown.append(f"Market EOD verdict unknown ({eod_detail})")

    summary, error = _probe(context, "flex summary", lambda: get_json(f"{flex_base}/flex/config/summary"))
    if isinstance(summary, dict):
        flex_source = str(summary.get("source") or "unknown")
    if flex_source == "unknown":
        unknown.append(f"Flex token source unknown ({error or 'no source in /flex/config/summary'})")

    flex_verdict, flex_reason = "unknown", "freshness-kpis not probed"
    kpis, error = _probe(
        context, "flex freshness", lambda: get_json(f"{flex_base}/flex/dashboard/freshness-kpis")
    )
    if error is None:
        max_age = float(env("FLEX_GATE_MAX_AGE_HOURS", str(DEFAULT_MAX_AGE_HOURS)))
        flex_verdict, flex_reason = flex_ingest_verdict(kpis, max_age_hours=max_age)
    else:
        flex_reason = f"freshness-kpis probe failed: {error}"
    if flex_verdict == "unknown":
        unknown.append(f"Flex ingest unknown ({flex_reason})")

    if flex_source == "none":
        raise RuntimeError("husbandry_gate: Flex source=none — block dbt")
    if flex_verdict in ("failed", "stale"):
        raise RuntimeError(f"husbandry_gate: Flex ingest {flex_verdict} ({flex_reason}) — block dbt")
    if eod_verdict == "critical":
        raise RuntimeError(
            f"husbandry_gate: Market EOD session {market_session} incomplete — {eod_detail}"
        )

    # The verdict must be about tonight's session, computed tonight (09-22 and 09-24
    # passed on the previous session's 'healthy', before the doctor was read fresh).
    expected = (
        date.fromisoformat(config.expected_session) if config.expected_session else expected_session()
    )
    if isinstance(doctor, dict):
        if market_session != expected.isoformat():
            unknown.append(
                f"doctor judged session {market_session}, the batch closes {expected.isoformat()}"
            )
        if market_generated_at == "unknown":
            unknown.append("doctor report carries no generated_at (not a fresh computation)")

    if unknown:
        if not config.allow_unknown:
            raise RuntimeError(
                "husbandry_gate: fails closed — "
                + "; ".join(unknown)
                + " (a hand-launched run may set config allow_unknown: true)"
            )
        context.log.warning("husbandry_gate: allow_unknown set, passing despite: %s", "; ".join(unknown))

    context.log.info(
        "husbandry_gate ok market=%s eod=%s session=%s expected=%s generated_at=%s "
        "flex_source=%s flex_ingest=%s (%s)",
        market_verdict,
        eod_verdict,
        market_session,
        expected.isoformat(),
        market_generated_at,
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
                "expected_session": expected.isoformat(),
                "market_generated_at": market_generated_at,
                "flex_source": flex_source,
                "flex_ingest": flex_verdict,
                "flex_ingest_reason": flex_reason,
                "gate": "pass" if not unknown else "pass (allow_unknown)",
                "overridden": "; ".join(unknown) if unknown else None,
            }
        )
    )


PLUGIN_BATCH_ASSETS = [
    market_eod,
    flex_trades,
    flex_transactions,
    husbandry_gate,
]
