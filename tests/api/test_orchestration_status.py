"""Unit tests for orchestration status + signal-health overall (no DB)."""

from datetime import datetime, timezone

from bifrost_research.api.orchestration import compute_orchestration_status
from bifrost_research.api.orchestration_schedules import (
    HUSBANDRY_SCHEDULE_JOBS,
    build_schedules_summary,
)
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


def test_schedules_summary_counts_and_failures() -> None:
    summary = build_schedules_summary(
        schedule_states={
            "research_trading_day_schedule": "RUNNING",
            "market_snapshot_schedule": "STOPPED",
            "market_option_refresh_schedule": "RUNNING",
        },
        last_runs={
            "research_trading_day": {
                "run_id": "a",
                "status": "SUCCESS",
                "ended_at": datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc),
            },
            "market_option_refresh_job": {
                "run_id": "b",
                "status": "FAILURE",
                "ended_at": datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc),
            },
        },
    )
    assert summary["schedules_total"] == len(HUSBANDRY_SCHEDULE_JOBS)
    assert summary["schedules_running"] == 2
    assert summary["schedules_stopped"] == 1
    assert len(summary["recent_failures"]) == 1
    assert summary["recent_failures"][0]["name"] == "market_option_refresh_schedule"
    snap = next(s for s in summary["schedules"] if s["name"] == "market_snapshot_schedule")
    assert snap["status"] == "STOPPED"
    assert snap["last_run_id"] is None
    assert snap["next_tick_at"] is None


def test_next_tick_at_running_cron_uses_schedule_timezone() -> None:
    from bifrost_research.api.orchestration_schedules import next_tick_at

    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)  # Monday 08:00 ET
    # Mon–Fri 22:30 America/New_York → 02:30Z the next calendar day (EDT).
    assert (
        next_tick_at("30 22 * * 1-5", status="RUNNING", now=now, tz="America/New_York")
        == "2026-09-15T02:30:00Z"
    )
    # Same cron in UTC stays 22:30Z the same day.
    assert next_tick_at("30 22 * * 1-5", status="RUNNING", now=now, tz="UTC") == "2026-09-14T22:30:00Z"
    assert next_tick_at("30 22 * * 1-5", status="STOPPED", now=now, tz="America/New_York") is None
    assert next_tick_at(None, status="RUNNING", now=now) is None
    # 04:00 ET Monday: 06:30 America/New_York (Mon–Sat) → 10:30Z the same day (EDT).
    morning = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
    assert (
        next_tick_at("30 6 * * 1-6", status="RUNNING", now=morning, tz="America/New_York")
        == "2026-09-14T10:30:00Z"
    )


def test_schedules_summary_computes_next_tick() -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    summary = build_schedules_summary(
        schedule_meta={
            "research_daily_digest_schedule": {
                "status": "RUNNING",
                "cron_schedule": "0 3 * * *",
            },
            "research_trading_day_schedule": {
                "status": "RUNNING",
                "cron_schedule": "30 22 * * 1-5",
            },
            "research_eod_review_schedule": {
                "status": "STOPPED",
                "cron_schedule": "30 22 * * 1-5",
            },
        },
        now=now,
    )
    digest = next(s for s in summary["schedules"] if s["name"] == "research_daily_digest_schedule")
    trading = next(s for s in summary["schedules"] if s["name"] == "research_trading_day_schedule")
    eod = next(s for s in summary["schedules"] if s["name"] == "research_eod_review_schedule")
    assert digest["next_tick_at"] == "2026-09-15T03:00:00Z"
    assert trading["next_tick_at"] == "2026-09-15T02:30:00Z"
    assert eod["next_tick_at"] is None
    assert eod["cron_schedule"] == "30 22 * * 1-5"
