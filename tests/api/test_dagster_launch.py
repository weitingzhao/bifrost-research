"""The agent triggers launch a Dagster job; they do not interpret a GraphQL error as success."""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import HTTPError

import pytest

from bifrost_research.api import dagster_launch


class _Resp(io.BytesIO):
    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *_a: Any) -> None:
        self.close()


def test_launch_job_reads_the_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "data": {
            "launchRun": {
                "__typename": "LaunchRunSuccess",
                "run": {"runId": "abc"},
            }
        }
    }

    def _open(req: Any, timeout: float = 0) -> _Resp:
        assert req.full_url.endswith("/graphql")
        sent = json.loads(req.data.decode())
        assert sent["variables"]["executionParams"]["selector"]["jobName"] == "research_daily_digest_job"
        assert timeout == 15.0
        return _Resp(json.dumps(body).encode())

    monkeypatch.setattr(dagster_launch.urllib.request, "urlopen", _open)
    out = dagster_launch.launch_job("research_daily_digest_job")
    assert out == {
        "ok": True,
        "job_name": "research_daily_digest_job",
        "run_id": "abc",
        "status": "launched",
    }


def test_launch_job_raises_the_dagster_message(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"data": {"launchRun": {"__typename": "PythonError", "message": "no such job"}}}

    def _open(req: Any, timeout: float = 0) -> _Resp:
        del req, timeout
        return _Resp(json.dumps(body).encode())

    monkeypatch.setattr(dagster_launch.urllib.request, "urlopen", _open)
    with pytest.raises(RuntimeError, match="no such job"):
        dagster_launch.launch_job("research_memory_distill_job")


def test_launch_job_surfaces_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _open(req: Any, timeout: float = 0) -> _Resp:
        del req, timeout
        raise HTTPError("http://dagster/graphql", 500, "nope", hdrs=None, fp=io.BytesIO(b""))  # type: ignore[arg-type]

    monkeypatch.setattr(dagster_launch.urllib.request, "urlopen", _open)
    with pytest.raises(HTTPError):
        dagster_launch.launch_job("research_weekly_policy_review_job")
