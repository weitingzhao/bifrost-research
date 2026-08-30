"""Unit tests for orchestration status + signal-health overall (no DB)."""

from datetime import datetime, timezone

from bifrost_research.api.orchestration import compute_orchestration_status
from bifrost_research.api.signal_health import _overall_from_freshness


def test_overall_counts_stale_as_degraded() -> None:
    assert (
        _overall_from_freshness(
            [{"status": "fresh"}, {"status": "stale"}],
        )
        == "degraded"
    )
    assert _overall_from_freshness([{"status": "fresh"}]) == "ok"


def test_orchestration_schema_missing() -> None:
    data = compute_orchestration_status(schema_missing=True)
    assert data["verdict"] == "unknown"
    assert data["detail"] == "ops_dagster schema missing"


def test_orchestration_permission_denied() -> None:
    data = compute_orchestration_status(permission_denied=True)
    assert data["verdict"] == "caution"
    assert "permission denied" in data["detail"]


def test_orchestration_table_missing() -> None:
    data = compute_orchestration_status(table_missing=True)
    assert data["verdict"] == "unknown"
    assert "table missing" in data["detail"]
    assert "not found" not in data["detail"]


def test_orchestration_error_fail_soft() -> None:
    data = compute_orchestration_status(error="boom")
    assert data["verdict"] == "caution"
    assert "boom" in data["detail"]


def test_orchestration_success_within_sla() -> None:
    now = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
    data = compute_orchestration_status(
        now=now,
        last_run={"run_id": "r1", "status": "SUCCESS", "ended_at": ended},
    )
    assert data["verdict"] == "healthy"


def test_orchestration_no_run_within_grace() -> None:
    now = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
    data = compute_orchestration_status(now=now, last_run=None)
    assert data["verdict"] == "caution"
    assert data["overdue"] is False
    assert "no research_trading_day runs" in data["detail"]
