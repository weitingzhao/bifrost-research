"""Dagster run failures and failed ERROR asset checks → Alertmanager, on the Bifrost route.

Alertmanager only forwards ``alertname=~"Bifrost.*"`` to the ops-agent webhook
(bifrost-trade-infra k8s/monitoring); anything else lands in a receiver with no
config. A failed schedule used to be visible only to whoever opened Dagster.
Fail-soft: a sensor that cannot reach Alertmanager logs and returns.

Note: do not use ``from __future__ import annotations`` — Dagster needs live
context types.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from dagster import (
    AssetCheckSeverity,
    DagsterEventType,
    DagsterRunStatus,
    DefaultSensorStatus,
    RunFailureSensorContext,
    RunStatusSensorContext,
    run_failure_sensor,
    run_status_sensor,
)

DEFAULT_ALERTMANAGER_URL = (
    "http://kube-prometheus-stack-alertmanager.monitoring.svc.cluster.local:9093"
)


def alertmanager_payload(job_name: str, run_id: str, message: str, *, now: datetime | None = None) -> list[dict]:
    """One Alertmanager v2 alert; ``alertname`` starts with Bifrost so the route matches."""
    started = now or datetime.now(timezone.utc)
    return [
        {
            "labels": {
                "alertname": "BifrostDagsterRunFailed",
                "severity": "warning",
                "namespace": "research",
                "job": job_name,
                "run_id": run_id,
            },
            "annotations": {
                "summary": f"Dagster run {job_name} failed",
                "description": message[:800],
            },
            "startsAt": started.isoformat().replace("+00:00", "Z"),
            "endsAt": (started + timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
        }
    ]


def post_alert(payload: list[dict], *, base_url: str | None = None, timeout: float = 10.0) -> bool:
    url = (base_url or os.environ.get("ALERTMANAGER_URL") or DEFAULT_ALERTMANAGER_URL).rstrip("/")
    req = urllib.request.Request(
        f"{url}/api/v2/alerts",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):
        return False


@run_failure_sensor(
    name="bifrost_run_failure_alert",
    default_status=DefaultSensorStatus.RUNNING,
    description="POST a BifrostDagsterRunFailed alert to Alertmanager for every failed run.",
)
def bifrost_run_failure_alert(context: RunFailureSensorContext) -> None:
    job_name = context.dagster_run.job_name
    run_id = context.dagster_run.run_id
    message = str(context.failure_event.message or "run failed")
    ok = post_alert(alertmanager_payload(job_name, run_id, message))
    if ok:
        context.log.info("alerted Alertmanager: %s run %s", job_name, run_id)
    else:
        context.log.warning("Alertmanager unreachable; failure of %s (%s) not alerted", job_name, run_id)


def failed_error_checks(instance: Any, run_id: str) -> list[tuple[str, str, str]]:
    """(asset, check, findings) of every ERROR-severity check that failed in ``run_id``.

    Output checks are non-blocking (TD-92): the run stays SUCCESS, so the run
    failure sensor never sees them. WARN checks stay on the asset page only.
    """
    records = instance.get_records_for_run(
        run_id, of_type=DagsterEventType.ASSET_CHECK_EVALUATION
    ).records
    out: list[tuple[str, str, str]] = []
    for record in records:
        event = record.event_log_entry.dagster_event
        data = event.event_specific_data if event is not None else None
        if data is None or data.passed or data.severity != AssetCheckSeverity.ERROR:
            continue
        findings = (data.metadata or {}).get("findings")
        out.append(
            (
                data.asset_key.to_user_string(),
                data.check_name,
                str(getattr(findings, "value", findings) or "check failed"),
            )
        )
    return out


def asset_check_payload(
    job_name: str,
    run_id: str,
    failures: list[tuple[str, str, str]],
    *,
    now: datetime | None = None,
) -> list[dict]:
    """One ``BifrostDagsterAssetCheckFailed`` alert per failed check."""
    started = now or datetime.now(timezone.utc)
    return [
        {
            "labels": {
                "alertname": "BifrostDagsterAssetCheckFailed",
                "severity": "warning",
                "namespace": "research",
                "job": job_name,
                "asset": asset,
                "check": check,
                "run_id": run_id,
            },
            "annotations": {
                "summary": f"Dagster check {asset}:{check} failed in {job_name}",
                "description": findings[:800],
            },
            "startsAt": started.isoformat().replace("+00:00", "Z"),
            "endsAt": (started + timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
        }
        for asset, check, findings in failures
    ]


@run_status_sensor(
    run_status=DagsterRunStatus.SUCCESS,
    name="bifrost_asset_check_alert",
    default_status=DefaultSensorStatus.RUNNING,
    description=(
        "POST a BifrostDagsterAssetCheckFailed alert for every ERROR-severity asset "
        "check that failed in a successful run (output checks do not fail the run)."
    ),
)
def bifrost_asset_check_alert(context: RunStatusSensorContext) -> None:
    run = context.dagster_run
    failures = failed_error_checks(context.instance, run.run_id)
    if not failures:
        return
    if post_alert(asset_check_payload(run.job_name, run.run_id, failures)):
        context.log.info("alerted Alertmanager: %d failed checks in %s", len(failures), run.run_id)
    else:
        context.log.warning(
            "Alertmanager unreachable; %d failed checks in %s not alerted", len(failures), run.run_id
        )


FAILURE_SENSORS = [bifrost_run_failure_alert, bifrost_asset_check_alert]
