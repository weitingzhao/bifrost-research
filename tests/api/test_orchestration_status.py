"""Unit tests for orchestration status + signal-health overall (no DB)."""

from datetime import datetime, timezone

import pytest

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
    # croniter lives in the `copilot` extra; without it the helper fails soft to
    # None by design, so asserting a time here would only test the extra.
    pytest.importorskip("croniter")
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
    pytest.importorskip("croniter")
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
    # The instigator rows above carry stale crons (a DECLARED_IN_CODE row keeps the
    # cron it was created with); the code's cron wins: digest 30 11 * * 1-5 UTC.
    assert digest["cron_schedule"] == "30 11 * * 1-5"
    assert digest["next_tick_at"] == "2026-09-15T11:30:00Z"
    assert trading["next_tick_at"] == "2026-09-15T02:30:00Z"
    assert eod["next_tick_at"] is None
    assert eod["cron_schedule"] == "30 21 * * 1-5"


def test_schedule_rows_carry_timezone_and_market_slots() -> None:
    """Console maps plugin slot -> schedule from these fields (TD-108)."""
    summary = build_schedules_summary(schedule_meta={})
    rows = {s["name"]: s for s in summary["schedules"]}
    assert rows["market_corporate_schedule"]["market_slots"] == ["corporate"]
    assert rows["market_universe_calendar_schedule"]["market_slots"] == [
        "universe-daily",
        "calendar",
        "stock-eod",
        "eod-pipeline",
    ]
    assert rows["research_trading_day_schedule"]["market_slots"] == []
    assert rows["market_intraday_chain_1030_schedule"]["execution_timezone"] == "America/New_York"
    slots = [slot for row in rows.values() for slot in row["market_slots"]]
    assert "option-trades" not in slots


@pytest.mark.parametrize('rows,verdict,detail', [
    ([('true','AssetCheckSeverity.WARN')], 'healthy', '1 pass, 0 WARN, 0 ERROR'),
    ([('false','AssetCheckSeverity.WARN')], 'caution', '0 pass, 1 WARN, 0 ERROR'),
    ([('false','AssetCheckSeverity.ERROR')], 'caution', '0 pass, 0 WARN, 1 ERROR'),
    ([], 'caution', 'unmeasured'),
])
def test_success_run_is_graded_by_its_output_checks(rows, verdict, detail):
    from bifrost_research.api.orchestration import _attach_output_checks
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params): assert params == ('run1',)
        def fetchall(self): return rows
    class Conn:
        def cursor(self): return Cursor()
    result = _attach_output_checks(Conn(), {'verdict':'healthy', 'last_run_id':'run1', 'detail':'completed'})
    assert result['verdict'] == verdict and detail in result['detail']


def test_unreadable_output_checks_never_report_healthy():
    from bifrost_research.api.orchestration import _attach_output_checks
    class Conn:
        def cursor(self): raise RuntimeError('permission denied')
        def rollback(self): self.rolled_back = True
    conn = Conn()
    result = _attach_output_checks(conn, {'verdict':'healthy', 'last_run_id':'run1', 'detail':'completed'})
    assert result['verdict'] == 'caution' and conn.rolled_back


@pytest.mark.parametrize('condition,expected', [
    ('missing','caution'), ('stopped','caution'), ('stale','caution'),
    ('pending','caution'), ('calendar','caution'), ('source','caution'),
    ('full','caution'), ('missed','caution'), ('unknown','caution'),
    ('corrupt','caution'), ('declared','caution'), ('measured','healthy'),
])
def test_alert_retry_health_never_falsely_green(condition, expected):
    import json
    from bifrost_research.api.orchestration import _attach_alert_delivery
    now = datetime.now(timezone.utc).timestamp()
    state = {'version':1,'pending':{},'incidents':{},'checked_at':now,
             'source_error':None,'deadline_error':None,'scan_blocked':False}
    if condition == 'stale':
        state['checked_at'] -= 301
    if condition == 'pending':
        state['pending']['k'] = {'queued_at':now-120}
    if condition == 'calendar':
        state['deadline_error'] = 'calendar incomplete'
    if condition == 'source':
        state['source_error'] = 'OSError'
    if condition == 'full':
        state['scan_blocked'] = True
    if condition == 'missed':
        state['incidents']['x'] = True
    status = 'STOPPED' if condition == 'stopped' else ('UNKNOWN' if condition == 'unknown' else 'RUNNING')
    if condition == 'declared':
        status = 'DECLARED_IN_CODE'
    rows = [] if condition == 'missing' else [(status,'broken' if condition == 'corrupt' else json.dumps(state))]
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def execute(self,q):
            assert q.startswith('SET LOCAL') or 'bifrost_alert_delivery' in q
        def fetchall(self): return rows
    class Conn:
        def cursor(self): return Cursor()
        def rollback(self): pass
    result = _attach_alert_delivery(Conn(), {'verdict':'healthy','detail':'batch passed'})
    assert result['verdict'] == expected
    assert 'Owner receipt unverified' in result['detail']


def test_alert_probe_failure_retains_overdue_precedence():
    from bifrost_research.api.orchestration import _attach_alert_delivery
    class Conn:
        def cursor(self): raise OSError('denied')
        def rollback(self): pass
    result = _attach_alert_delivery(Conn(), {'verdict':'missed','detail':'overdue','overdue':True})
    assert result['verdict'] == 'missed' and result['overdue']
    assert 'notification unverified' in result['detail']
