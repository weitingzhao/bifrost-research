"""research-api /metrics: scheduler liveness as timestamps (TD-99)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from bifrost_research.api import metrics
from bifrost_research.api.schedule_roster import SCHEDULE_ROSTER

NOW = datetime(2026, 10, 6, 16, 10, tzinfo=timezone.utc)  # Tuesday


def _body(name: str) -> str:
    return json.dumps({"origin": {"job_name": name}})


def _series(families, name: str) -> dict[tuple, float]:
    fam = next(f for f in families if f.name == name)
    return {tuple(sorted(labels.items())): value for labels, value in fam.samples}


def test_fold_keeps_the_newest_state_and_the_newest_tick_over_duplicate_rows() -> None:
    rows = [
        (_body("research_trading_day_schedule"), "RUNNING", 100.0, 100.0, None, None),
        (_body("research_trading_day_schedule"), "RUNNING", 200.0, 900.0, 800.0, "SUCCESS"),
        ("not json", "RUNNING", 1.0, 1.0, None, None),
    ]
    folded = metrics.fold_instigator_rows(rows)
    assert folded == {
        "research_trading_day_schedule": {
            "status": "RUNNING",
            "updated": 900.0,
            "created": 100.0,
            "tick": 800.0,
            "tick_status": "SUCCESS",
        }
    }


def test_last_due_follows_the_code_cron_and_timezone() -> None:
    pytest.importorskip("croniter")
    # 22:30 America/New_York Mon-Fri: Monday's fire is 02:30Z Tuesday.
    assert metrics.last_due_at("30 22 * * 1-5", "America/New_York", NOW) == datetime(
        2026, 10, 6, 2, 30, tzinfo=timezone.utc
    ).timestamp()
    # Weekly Sunday 07:30 UTC: on Tuesday the last fire is two days back.
    assert metrics.last_due_at("30 7 * * 0", "UTC", NOW) == datetime(
        2026, 10, 4, 7, 30, tzinfo=timezone.utc
    ).timestamp()


def test_every_roster_schedule_gets_a_series_and_missing_ones_are_flagged() -> None:
    pytest.importorskip("croniter")
    present = {
        "market_corporate_schedule": {
            "status": "DECLARED_IN_CODE",
            "updated": 1.0,
            "created": 10.0,
            "tick": 123.0,
            "tick_status": "SUCCESS",
        },
        "research_eod_review_schedule": {
            "status": "STOPPED",
            "updated": 1.0,
            "created": 10.0,
            "tick": None,
            "tick_status": None,
        },
    }
    fams = metrics.build_schedule_families(present, {"market_corporate_job": (50.0, None)}, NOW)
    running = _series(fams, "bifrost_dagster_schedule_running")
    assert len(running) == len(SCHEDULE_ROSTER)
    corp = (("job", "market_corporate_job"), ("schedule", "market_corporate_schedule"))
    assert running[corp + (("state", "RUNNING"),)] == 1.0
    eod = (("job", "research_eod_review_job"), ("schedule", "research_eod_review_schedule"))
    assert running[eod + (("state", "STOPPED"),)] == 0.0
    # Renamed in code / never loaded by the daemon: present in the roster, absent in the DB.
    digest = (("job", "research_daily_digest_job"), ("schedule", "research_daily_digest_schedule"))
    assert running[digest + (("state", "missing"),)] == 0.0

    ticks = _series(fams, "bifrost_dagster_schedule_last_tick_timestamp_seconds")
    assert ticks[corp] == 123.0
    assert ticks[eod] == 0.0, "never ticked must read as 0, not vanish"
    dues = _series(fams, "bifrost_dagster_schedule_last_due_timestamp_seconds")
    assert len(dues) == len(SCHEDULE_ROSTER)
    success = _series(fams, "bifrost_dagster_job_last_success_timestamp_seconds")
    assert success == {(("job", "market_corporate_job"),): 50.0}


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn
        self.rows: list = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        if sql.startswith("SET LOCAL"):
            return
        if "daemon_heartbeats" in sql:
            if self.conn.fail_heartbeats:
                raise RuntimeError("permission denied for table daemon_heartbeats")
            self.rows = [("SCHEDULER", 1791303060.0)]
        elif "ops_dagster.instigators" in sql:
            self.rows = [
                (_body("market_corporate_schedule"), "DECLARED_IN_CODE", 1.0, 2.0, 3.0, "SUCCESS")
            ]
        elif "ops_dagster.runs" in sql:
            self.rows = [("market_corporate_job", 4.0, None)]
        else:
            self.rows = [(5.0, 6.0, 7.0)]

    def fetchall(self) -> list:
        return self.rows


class _Conn:
    def __init__(self, fail_heartbeats: bool = False) -> None:
        self.fail_heartbeats = fail_heartbeats

    def cursor(self) -> _Cur:
        return _Cur(self)

    def rollback(self) -> None:
        return None


def test_collect_renders_prometheus_text() -> None:
    pytest.importorskip("croniter")
    text = metrics.collect(_Conn(), now=NOW)
    assert 'bifrost_dagster_daemon_heartbeat_timestamp_seconds{daemon_type="SCHEDULER"} 1791303060' in text
    assert 'bifrost_research_metrics_probe_ok{probe="schedules"} 1' in text
    assert 'bifrost_research_event_radar_timestamp_seconds{signal="sec_filing_fetched"} 5' in text
    assert (
        'bifrost_dagster_schedule_last_tick_status{job="market_corporate_job",'
        'schedule="market_corporate_schedule",status="SUCCESS"} 1'
    ) in text
    for line in text.splitlines():
        assert line.startswith("#") or line.startswith("bifrost_"), line


def test_a_failing_probe_reports_zero_and_keeps_the_others() -> None:
    text = metrics.collect(_Conn(fail_heartbeats=True), now=NOW)
    assert 'bifrost_research_metrics_probe_ok{probe="daemon_heartbeats"} 0' in text
    assert "bifrost_dagster_daemon_heartbeat_timestamp_seconds{" not in text
    assert 'bifrost_research_metrics_probe_ok{probe="event_radar"} 1' in text
