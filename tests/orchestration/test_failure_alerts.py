"""Failed runs reach Alertmanager on the Bifrost route; nothing else is routed."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("dagster")

from bifrost_research.orchestration.failure_alerts import alertmanager_payload, post_alert


def test_payload_is_on_the_bifrost_route() -> None:
    now = datetime(2026, 9, 6, 4, 0, tzinfo=timezone.utc)
    [alert] = alertmanager_payload("market_universe_calendar_job", "abc123", "RemoteDisconnected", now=now)
    assert alert["labels"]["alertname"].startswith("Bifrost")
    assert alert["labels"]["job"] == "market_universe_calendar_job"
    assert alert["labels"]["run_id"] == "abc123"
    assert alert["startsAt"] == "2026-09-06T04:00:00Z"
    assert alert["endsAt"] == "2026-09-06T10:00:00Z"
    assert "RemoteDisconnected" in alert["annotations"]["description"]


def test_post_alert_is_fail_soft() -> None:
    assert post_alert([{"labels": {"alertname": "BifrostTest"}}], base_url="http://127.0.0.1:9", timeout=0.5) is False
