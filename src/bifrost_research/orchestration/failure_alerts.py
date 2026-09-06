"""Dagster run failures → Alertmanager, on the Bifrost route.

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

from dagster import DefaultSensorStatus, RunFailureSensorContext, run_failure_sensor

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


FAILURE_SENSORS = [bifrost_run_failure_alert]
