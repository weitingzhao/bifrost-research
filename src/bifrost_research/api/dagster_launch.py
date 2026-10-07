"""Start a Dagster job from research-api. The API does not run the engine.

Agent and distill triggers used to call the engine in this process, with no run
record, and could race the schedule. They now launch the same job the schedule
uses. GraphQL is the Dagster webserver; the job's code stays in the daemon.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

_LAUNCH = """
mutation LaunchRun($executionParams: ExecutionParams!) {
  launchRun(executionParams: $executionParams) {
    __typename
    ... on LaunchRunSuccess { run { runId } }
    ... on RunConfigValidationInvalid { errors { message } }
    ... on InvalidSubsetError { message }
    ... on PythonError { message }
    ... on UnauthorizedError { message }
    ... on ConflictingExecutionParamsError { message }
  }
}
"""


def graphql_url() -> str:
    return (
        os.environ.get("DAGSTER_GRAPHQL_URL")
        or "http://dagster-webserver.research.svc.cluster.local:3000/graphql"
    ).strip()


def launch_job(job_name: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Launch ``job_name`` and return ``{ok, job_name, run_id, status}``."""
    body = {
        "query": _LAUNCH,
        "variables": {
            "executionParams": {
                "selector": {
                    "repositoryLocationName": (
                        os.environ.get("DAGSTER_REPOSITORY_LOCATION")
                        or "bifrost_research.orchestration.definitions"
                    ).strip(),
                    "repositoryName": (
                        os.environ.get("DAGSTER_REPOSITORY_NAME") or "__repository__"
                    ).strip(),
                    "jobName": job_name,
                }
            }
        },
    }
    req = urllib.request.Request(
        graphql_url(),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = (payload.get("data") or {}).get("launchRun") or {}
    if data.get("__typename") == "LaunchRunSuccess":
        run = data.get("run") or {}
        return {
            "ok": True,
            "job_name": job_name,
            "run_id": run.get("runId"),
            "status": "launched",
        }
    message = data.get("message")
    errors = data.get("errors")
    if not message and isinstance(errors, list):
        message = "; ".join(str(item.get("message") or item) for item in errors if isinstance(item, dict))
    graph_errors = payload.get("errors")
    if not message and isinstance(graph_errors, list) and graph_errors:
        message = str(graph_errors[0].get("message") or graph_errors[0])
    raise RuntimeError(message or f"Dagster launch failed ({data.get('__typename') or 'empty'})")
