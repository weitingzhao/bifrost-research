"""No external sends: real Dagster persistence, bounded replay and deadline faults."""

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("dagster")

from dagster import DagsterInstance, DagsterRunStatus, build_sensor_context, job
from dagster._core.remote_origin import (
    RegisteredCodeLocationOrigin,
    RemoteInstigatorOrigin,
    RemoteRepositoryOrigin,
)
from dagster._core.scheduler.instigation import (
    InstigatorState,
    InstigatorStatus,
    InstigatorType,
    SensorInstigatorData,
)

from bifrost_research.orchestration import alert_delivery as delivery
from bifrost_research.orchestration.failure_alerts import alertmanager_payload

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
FIRE = datetime(2026, 10, 10, 2, 30, tzinfo=timezone.utc)


def queued(now=NOW, run_id="r1"):
    state = delivery._load(None, now)
    assert delivery._enqueue(state, alertmanager_payload("job", run_id, "failure", now=now), now)
    return state


def test_restart_replays_failed_delivery_and_dedups_real_dagster_storage(tmp_path, monkeypatch):
    """Commit evaluate_tick cursor via the same instance state store used by the daemon."""

    @job
    def failed_job():
        pass

    origin = RemoteInstigatorOrigin(
        RemoteRepositoryOrigin(RegisteredCodeLocationOrigin("local-test"), "test-repo"),
        delivery.SENSOR_NAME,
    )
    monkeypatch.setattr(delivery, "_read_calendar", lambda now: [now - timedelta(hours=3)])
    monkeypatch.setattr(delivery, "_deadlines", lambda *args: None)
    calls = []
    monkeypatch.setattr(
        delivery, "post_alert", lambda payload, **kw: calls.append(payload) or False
    )
    with DagsterInstance.local_temp(str(tmp_path)) as instance:
        run = instance.create_run_for_job(failed_job)
        instance.report_run_failed(run)
        initial = InstigatorState(
            origin,
            InstigatorType.SENSOR,
            InstigatorStatus.RUNNING,
            SensorInstigatorData(cursor=None),
        )
        instance.add_instigator_state(initial)
        result = delivery.bifrost_alert_delivery.evaluate_tick(
            build_sensor_context(instance=instance)
        )
        assert not calls, "newly discovered items must persist before first send"
        discovered = json.loads(result.cursor)
        assert len(discovered["pending"]) == 1
        instance.update_instigator_state(
            initial.with_data(SensorInstigatorData(cursor=result.cursor))
        )
        failed = delivery.bifrost_alert_delivery.evaluate_tick(
            build_sensor_context(instance=instance, cursor=result.cursor)
        )
        assert len(calls) == 1
        state = json.loads(failed.cursor)
        assert next(iter(state["pending"].values()))["attempts"] == 1
        # Test clock advances to next retry before saving; state persists to SQLite.
        for item in state["pending"].values():
            item["next_at"] = 0
        instance.update_instigator_state(
            initial.with_data(SensorInstigatorData(cursor=json.dumps(state)))
        )
    monkeypatch.setattr(delivery, "post_alert", lambda payload, **kw: calls.append(payload) or True)
    with DagsterInstance.local_temp(str(tmp_path)) as restarted:
        saved = restarted.get_instigator_state(origin.get_id(), initial.selector_id)
        result = delivery.bifrost_alert_delivery.evaluate_tick(
            build_sensor_context(instance=restarted, cursor=saved.sensor_instigator_data.cursor)
        )
        assert len(calls) == 2 and calls[0][0]["labels"] == calls[1][0]["labels"]
        assert json.loads(result.cursor)["pending"] == {}
        again = delivery.bifrost_alert_delivery.evaluate_tick(
            build_sensor_context(instance=restarted, cursor=result.cursor)
        )
        assert json.loads(again.cursor)["pending"] == {} and len(calls) == 2


def test_retry_backoff_and_expired_payload_refresh_without_drop(monkeypatch):
    state = queued()
    calls = []
    monkeypatch.setattr(delivery, "post_alert", lambda p, **kw: calls.append(p) or False)
    delivery._deliver(state, NOW)
    item = next(iter(state["pending"].values()))
    assert item["next_at"] == NOW.timestamp() + 60
    delivery._deliver(state, NOW + timedelta(seconds=59))
    assert len(calls) == 1
    for attempt in range(12):
        at = datetime.fromtimestamp(item["next_at"], timezone.utc)
        delivery._deliver(state, at)
        assert item["next_at"] - at.timestamp() <= 3600
    assert item["attempts"] == 13 and len(state["pending"]) == 1
    assert calls[-1][0]["startsAt"] == calls[0][0]["startsAt"]
    assert calls[-1][0]["endsAt"] != calls[0][0]["endsAt"]


def test_crash_after_http_replays_same_identity(monkeypatch):
    committed = json.dumps(queued())
    calls = []
    monkeypatch.setattr(delivery, "post_alert", lambda p, **kw: calls.append(p) or True)
    delivery._deliver(delivery._load(committed, NOW), NOW)
    # Crash before the tick's new cursor commits: replay the previous committed cursor.
    delivery._deliver(delivery._load(committed, NOW), NOW)
    assert len(calls) == 2 and calls[0][0]["labels"] == calls[1][0]["labels"]


@pytest.mark.parametrize("cursor", ["broken", "{}", '{"version":2}', "null"])
def test_invalid_cursor_never_resets_or_sends(cursor, monkeypatch):
    monkeypatch.setattr(delivery, "post_alert", lambda *a, **k: pytest.fail("must not send"))
    with pytest.raises(RuntimeError, match="preserve state"):
        delivery._load(cursor, NOW)


@pytest.mark.parametrize("change", ["missing-key", "nan", "wrong-identity"])
def test_malformed_saved_state_is_rejected_before_delivery(change):
    state = queued()
    if change == "missing-key":
        del state["deadline_error"]
    elif change == "nan":
        next(iter(state["pending"].values()))["next_at"] = float("nan")
    else:
        state["pending"]["wrong-key"] = state["pending"].pop(next(iter(state["pending"])))
    with pytest.raises(RuntimeError, match="preserve state"):
        delivery._load(json.dumps(state), NOW)


def test_queue_full_does_not_acknowledge_source_event(monkeypatch):
    state = delivery._load(None, NOW)
    for i in range(200):
        delivery._enqueue(state, alertmanager_payload("j", str(i), ""), NOW)
    record = SimpleNamespace(
        storage_id=7, event_log_entry=SimpleNamespace(run_id="new", timestamp=NOW.timestamp())
    )

    class Instance:
        def fetch_run_status_changes(self, filter, **kw):
            assert kw == {"limit": 20, "ascending": True}
            return SimpleNamespace(
                records=[record] if filter.event_type.name == "RUN_FAILURE" else []
            )

        def get_run_by_id(self, run_id):
            return SimpleNamespace(job_name="job", run_id=run_id)

    delivery._discover(Instance(), state, NOW)
    assert state["scan_blocked"] and state["streams"]["failure"] == 0
    assert len(state["pending"]) == 200
    # Once space opens, the same event is safely discovered rather than lost.
    state["pending"].clear()
    delivery._discover(Instance(), state, NOW)
    assert state["streams"]["failure"] == 7 and len(state["pending"]) == 1


def test_source_failure_keeps_pending_and_watermark():
    state = queued()

    class Instance:
        def fetch_run_status_changes(self, *a, **k):
            raise OSError("secret URL")

    delivery._discover(Instance(), state, NOW)
    assert state["source_error"] == "OSError" and len(state["pending"]) == 1
    assert state["streams"] == {"success": 0, "failure": 0}
    assert "secret" not in json.dumps(state)


def test_success_error_checks_queued_deduped_and_no_findings_leak(monkeypatch):
    state = delivery._load(None, NOW)
    record = SimpleNamespace(
        storage_id=8, event_log_entry=SimpleNamespace(run_id="r", timestamp=NOW.timestamp())
    )

    class Instance:
        def fetch_run_status_changes(self, filter, **kw):
            return SimpleNamespace(
                records=[record] if filter.event_type.name == "RUN_SUCCESS" else []
            )

        def get_run_by_id(self, run_id):
            return SimpleNamespace(job_name="job", run_id=run_id)

    monkeypatch.setattr(
        delivery, "failed_error_checks", lambda *a: [("a", "c", "token=secret")] * 2
    )
    delivery._discover(Instance(), state, NOW)
    assert len(state["pending"]) == 1 and state["streams"]["success"] == 8
    assert "secret" not in json.dumps(state)
    assert (
        next(iter(state["pending"].values()))["alert"]["labels"]["alertname"]
        == "BifrostDagsterAssetCheckFailed"
    )


def instance_with(status=None, manual=False):
    records = (
        []
        if status is None
        else [
            SimpleNamespace(
                dagster_run=SimpleNamespace(
                    status=status,
                    tags={}
                    if manual
                    else {"dagster/schedule_name": "research_trading_day_schedule"},
                )
            )
        ]
    )
    return SimpleNamespace(get_run_records=lambda **kw: records)


@pytest.mark.parametrize(
    "status", [None, DagsterRunStatus.QUEUED, DagsterRunStatus.STARTED, DagsterRunStatus.FAILURE]
)
def test_missing_stuck_and_failed_daily_batch_incidents_dedup(status):
    state = delivery._load(None, NOW)
    for _ in range(3):
        delivery._deadlines(instance_with(status), state, NOW, lambda now: [FIRE])
    assert len(state["pending"]) == 1 and len(state["incidents"]) == 1
    assert not state["deadline_error"]
    alert = next(iter(state["pending"].values()))["alert"]
    assert alert["labels"]["session"] == "2026-10-09" and "run_id" not in alert["labels"]


def test_manual_success_cannot_mask_missed_schedule_and_late_recovery_resolves():
    state = delivery._load(None, NOW)
    delivery._deadlines(
        instance_with(DagsterRunStatus.SUCCESS, manual=True), state, NOW, lambda now: [FIRE]
    )
    assert len(state["incidents"]) == 1
    delivery._deadlines(instance_with(DagsterRunStatus.SUCCESS), state, NOW, lambda now: [FIRE])
    assert not state["incidents"] and len(state["pending"]) == 1
    assert next(iter(state["pending"].values()))["resolved"]


def test_missed_dates_between_ticks_are_not_skipped_and_long_gap_unverified():
    state = delivery._load(None, NOW)
    state["last_fire"] = (FIRE - timedelta(days=2)).timestamp()
    delivery._deadlines(
        instance_with(),
        state,
        NOW,
        lambda now: [FIRE - timedelta(days=2), FIRE - timedelta(days=1), FIRE],
    )
    assert len(state["incidents"]) == 2
    state["last_fire"] = (FIRE - timedelta(days=20)).timestamp()
    delivery._deadlines(instance_with(), state, NOW, lambda now: [FIRE])
    assert "operator reconciliation" in state["deadline_error"]
    assert len(state["incidents"]) == 2


def test_calendar_fault_does_not_drop_retry_queue_or_fabricate_incident():
    state = queued()

    def unavailable(now):
        raise OSError("secret")

    delivery._deadlines(instance_with(), state, NOW, unavailable)
    assert state["deadline_error"] == "OSError" and not state["incidents"]
    assert len(state["pending"]) == 1


def test_unexpected_runtime_deadline_error_is_redacted():
    state = queued()

    def unavailable(now):
        raise RuntimeError("vendor URL with token=secret")

    delivery._deadlines(instance_with(), state, NOW, unavailable)
    assert state["deadline_error"] == "RuntimeError"
    assert "secret" not in json.dumps(state)


def test_tick_recovery_cancels_obsolete_firing_before_send(monkeypatch):
    state = delivery._load(None, NOW)
    delivery._deadlines(instance_with(), state, NOW, lambda now: [FIRE])
    calls = []
    monkeypatch.setattr(delivery, "post_alert", lambda p, **kw: calls.append(p) or True)
    committed = set(state["pending"])
    delivery._deadlines(instance_with(DagsterRunStatus.SUCCESS), state, NOW, lambda now: [FIRE])
    delivery._deliver(state, NOW, committed)
    assert not calls, "never send old firing after observed recovery; persist new resolution first"
    delivery._deliver(state, NOW + timedelta(minutes=1), set(state["pending"]))
    assert len(calls) == 1 and not state["pending"]


def test_calendar_weekend_holiday_and_dst_are_measured():
    # Monday after DST ends: 22:30 EST is 03:30 UTC. Holiday remains closed.
    now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    rows = [(date(2026, 10, 19) + timedelta(days=i), True) for i in range(15)]
    rows = [(d, d.weekday() < 5 and d != date(2026, 10, 30)) for d, _ in rows]

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, *a):
            pass

        def fetchall(self):
            return rows

    conn = SimpleNamespace(cursor=lambda: Cursor())
    fires = delivery._calendar_fires(conn, now)
    assert fires[-1].isoformat() == "2026-11-03T03:30:00+00:00"
    assert all(f.astimezone(delivery._ET).date() != date(2026, 10, 30) for f in fires)
    rows.pop()
    with pytest.raises(RuntimeError, match="calendar incomplete"):
        delivery._calendar_fires(conn, now)


def test_send_budget_is_bounded(monkeypatch):
    state = delivery._load(None, NOW)
    for i in range(15):
        delivery._enqueue(state, alertmanager_payload("j", str(i), ""), NOW)
    calls = []
    monkeypatch.setattr(delivery, "post_alert", lambda p, **kw: calls.append(kw) or False)
    delivery._deliver(state, NOW)
    assert len(calls) == 10 and all(c["timeout"] == 2 for c in calls)
