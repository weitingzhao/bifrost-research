"""Nightly self-heal for the Market Data Plugin — doctor → heal → drain → recheck.

The schedules say when slots fire; this asset checks, after the EOD chain has
had time to drain, that the session's rows are actually in the tables, and
executes the Plugin doctor's prescriptions when they are not. It raises only
when the recheck is still critical, so the failure sensor pages a human for
what could not be fixed by re-enqueueing. No retry policy: a second run would
just enqueue the same jobs again.

Note: do not use ``from __future__ import annotations`` — Dagster needs live context types.
"""

import time
from typing import Any

from dagster import (
    AssetExecutionContext,
    AssetKey,
    AssetSelection,
    DefaultScheduleStatus,
    MaterializeResult,
    ScheduleDefinition,
    asset,
    define_asset_job,
)

from bifrost_research.orchestration.market_slot_schedules import GROUP
from bifrost_research.orchestration.plugin_http import env, get_json, meta, post_json

# 00:45 UTC Tue–Sat = 20:45 EDT / 19:45 EST on the trading day: after the EOD
# chain (22:00 UTC) and the Plugin doctor's 19:30 New York session cutoff.
SELF_HEAL_CRON = "45 0 * * 2-6"
DEFAULT_WAIT_SEC = 900
POLL_SEC = 30


def should_heal(report: dict[str, Any]) -> bool:
    """Heal when the doctor found something and has at least one prescription."""
    return str(report.get("verdict") or "") != "healthy" and bool(report.get("prescriptions"))


def queue_drained(summary: dict[str, Any]) -> bool:
    """queue-summary → True when nothing is pending or running."""
    return int(summary.get("pending") or 0) == 0 and int(summary.get("running") or 0) == 0


def outcome(before: dict[str, Any], after: dict[str, Any] | None) -> str:
    """One word for the run: healthy | healed | degraded | critical."""
    b = str(before.get("verdict") or "unknown")
    if after is None:
        return b
    a = str(after.get("verdict") or "unknown")
    if a == "healthy":
        return "healthy" if b == "healthy" else "healed"
    return a


@asset(
    key=AssetKey(["batch", "market", "market_self_heal"]),
    group_name=GROUP,
    description=(
        "UTC 00:45 Tue–Sat — Plugin doctor for the day's session; executes its "
        "prescriptions, waits for the queue to drain, rechecks; fails only when still critical"
    ),
)
def market_self_heal(context: AssetExecutionContext) -> MaterializeResult:
    base = env(
        "MARKET_DATA_API_URL",
        "http://market-data-api.plugin-market-data.svc.cluster.local:8790",
    ).rstrip("/")
    wait_sec = int(env("MARKET_SELF_HEAL_WAIT_SEC", str(DEFAULT_WAIT_SEC)))

    before = get_json(f"{base}/market/doctor?probes=true", timeout=180.0)
    context.log.info(
        "doctor session=%s verdict=%s summary=%s prescriptions=%d",
        before.get("session"),
        before.get("verdict"),
        before.get("summary"),
        len(before.get("prescriptions") or []),
    )
    for f in before.get("findings") or []:
        if f.get("severity") != "ok":
            context.log.warning("[%s] %s — %s", f.get("severity"), f.get("title"), f.get("detail"))

    if not should_heal(before):
        return MaterializeResult(
            metadata=meta(
                {
                    "session": before.get("session"),
                    "verdict_before": before.get("verdict"),
                    "outcome": outcome(before, None),
                    "healed": False,
                    "summary": before.get("summary"),
                }
            )
        )

    token = env("MARKET_DATA_WRITE_TOKEN")
    if not token:
        raise RuntimeError("MARKET_DATA_WRITE_TOKEN required to heal the market queue")
    healed = post_json(
        f"{base}/market/doctor/heal",
        {"dry_run": False},
        token_header="X-Market-Data-Write-Token",
        token=token,
        timeout=180.0,
    )
    for action in healed.get("actions") or []:
        context.log.info(
            "heal %s → %s", {k: v for k, v in action.items() if k != "result"}, action.get("result")
        )

    deadline = time.monotonic() + wait_sec
    drained = False
    while time.monotonic() < deadline:
        time.sleep(POLL_SEC)
        try:
            summary = get_json(f"{base}/market/ingest/queue-summary")
        except Exception as exc:  # noqa: BLE001 — keep waiting on a transient API blip
            context.log.warning("queue-summary probe failed: %s", exc)
            continue
        if queue_drained(summary):
            drained = True
            break
        context.log.info(
            "queue pending=%s running=%s", summary.get("pending"), summary.get("running")
        )

    after = get_json(f"{base}/market/doctor?probes=false", timeout=180.0)
    result = outcome(before, after)
    context.log.info("recheck verdict=%s (%s) drained=%s", after.get("verdict"), result, drained)
    md = meta(
        {
            "session": before.get("session"),
            "verdict_before": before.get("verdict"),
            "verdict_after": after.get("verdict"),
            "outcome": result,
            "healed": True,
            "enqueued": healed.get("enqueued"),
            "drained": drained,
            "summary": after.get("summary"),
        }
    )
    if result == "critical":
        still = [f.get("title") for f in after.get("findings") or [] if f.get("severity") == "crit"]
        raise RuntimeError(f"market self-heal: still critical after heal — {still}")
    return MaterializeResult(metadata=md)


market_self_heal_job = define_asset_job(
    name="market_self_heal_job",
    selection=AssetSelection.assets(market_self_heal),
    description="Massive doctor → heal → recheck (UTC) — the nightly self-heal",
)

market_self_heal_schedule = ScheduleDefinition(
    name="market_self_heal_schedule",
    job=market_self_heal_job,
    cron_schedule=SELF_HEAL_CRON,
    execution_timezone="UTC",
    default_status=DefaultScheduleStatus.RUNNING,
    description="Massive doctor → heal → recheck (UTC) — the nightly self-heal",
)
