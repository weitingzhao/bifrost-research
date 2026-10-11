"""TD-304: two-phase, at-least-once delivery using Dagster's persisted sensor cursor.

Discovery never sends its newly discovered alerts in the same tick. A successful
tick commits the outbox before the following tick can send. A crash after POST
can still replay: stable Alertmanager labels provide receiver-side deduplication.
Transport acceptance is not an Owner receipt. No new database or dependency.
"""

import hashlib
import json
import math
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dagster import (
    DagsterEventType,
    DagsterRunStatus,
    DefaultSensorStatus,
    RunStatusChangeRecordsFilter,
    RunsFilter,
    SensorEvaluationContext,
    SkipReason,
    sensor,
)

from bifrost_research.db.conn import connect
from bifrost_research.orchestration.failure_alerts import (
    alertmanager_payload,
    asset_check_payload,
    failed_error_checks,
    post_alert,
)

SENSOR_NAME = "bifrost_alert_delivery"
_CAPACITY = 200
_PAGE = 20
_SEND_LIMIT = 10
_ET = ZoneInfo("America/New_York")


def _iso(now):
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _load(cursor, now):
    if cursor is None:
        return {
            "version": 1,
            "since": now.timestamp() - 86400,
            "streams": {"failure": 0, "success": 0},
            "pending": {},
            "recent": [],
            "incidents": {},
            "last_fire": None,
            "source_error": None,
            "deadline_error": None,
            "scan_blocked": False,
            "checked_at": now.timestamp(),
        }
    try:
        state = json.loads(cursor)
        valid = (
            state["version"] == 1
            and isinstance(state["pending"], dict)
            and len(state["pending"]) <= _CAPACITY
            and isinstance(state["recent"], list)
            and len(state["recent"]) <= 512
            and isinstance(state["incidents"], dict)
            and len(state["incidents"]) <= 64
            and type(state["scan_blocked"]) is bool
            and all(
                state[field] is None or isinstance(state[field], str)
                for field in ("source_error", "deadline_error")
            )
            and all(
                isinstance(state[field], (float, int)) and math.isfinite(state[field])
                for field in ("since", "checked_at")
            )
            and (state["last_fire"] is None or math.isfinite(state["last_fire"]))
        )
        for stream in ("failure", "success"):
            valid = (
                valid and type(state["streams"][stream]) is int and state["streams"][stream] >= 0
            )
        for key, item in state["pending"].items():
            valid = (
                valid
                and isinstance(item["alert"]["labels"], dict)
                and type(item["attempts"]) is int
                and item["attempts"] >= 0
                and all(
                    isinstance(item[field], (int, float)) and math.isfinite(item[field])
                    for field in ("next_at", "queued_at")
                )
                and type(item["resolved"]) is bool
                and key == _key(item["alert"], item["resolved"])
            )
        valid = valid and all(math.isfinite(float(key)) for key in state["incidents"])
        if not valid:
            raise ValueError("unsupported cursor state")
        return state
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        # Never silently reset an unreadable cursor and lose pending alerts.
        raise RuntimeError(
            "alert delivery cursor invalid; preserve state for operator review"
        ) from exc


def _key(alert, resolved=False):
    labels = json.dumps(alert["labels"], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((labels + str(resolved)).encode()).hexdigest()


def _enqueue(state, alerts, now, resolved=False):
    fresh = {
        _key(alert, resolved): alert
        for alert in alerts
        if _key(alert, resolved) not in state["pending"]
        and _key(alert, resolved) not in state["recent"]
    }
    if len(state["pending"]) + len(fresh) > _CAPACITY:
        state["scan_blocked"] = True
        return False
    for key, alert in fresh.items():
        state["pending"][key] = {
            "alert": alert,
            "attempts": 0,
            "queued_at": now.timestamp(),
            "next_at": now.timestamp(),
            "resolved": resolved,
        }
    return True


def _deliver(state, now, committed=None):
    # Existing committed items only. No finite retry count silently drops work.
    due = [
        (key, item)
        for key, item in state["pending"].items()
        if item["next_at"] <= now.timestamp() and (committed is None or key in committed)
    ]
    due.sort(key=lambda pair: pair[1]["next_at"])
    for key, item in due[:_SEND_LIMIT]:
        alert = dict(item["alert"])
        alert["endsAt"] = _iso(now if item["resolved"] else now + timedelta(hours=6))
        try:
            accepted = post_alert([alert], timeout=2.0)
        except Exception:
            accepted = False
        if accepted is True:
            del state["pending"][key]
            state["recent"] = (state["recent"] + [key])[-512:]
        else:
            item["attempts"] += 1
            item["next_at"] = now.timestamp() + min(3600, 60 * 2 ** min(item["attempts"] - 1, 6))


def _discover(instance, state, now):
    state["source_error"] = None
    state["scan_blocked"] = False
    for stream, event_type in (
        ("failure", DagsterEventType.RUN_FAILURE),
        ("success", DagsterEventType.RUN_SUCCESS),
    ):
        try:
            page = instance.fetch_run_status_changes(
                RunStatusChangeRecordsFilter(
                    event_type=event_type,
                    after_storage_id=state["streams"][stream],
                    after_timestamp=state["since"],
                ),
                limit=_PAGE,
                ascending=True,
            )
            for record in page.records:
                entry = record.event_log_entry
                run = instance.get_run_by_id(entry.run_id)
                if run is None:
                    raise RuntimeError("event run unavailable")
                if stream == "failure":
                    # Raw failure messages may contain vendor URLs or tokens.
                    alerts = alertmanager_payload(
                        run.job_name,
                        run.run_id,
                        "Run failed; inspect the restricted Dagster event log.",
                        now=datetime.fromtimestamp(entry.timestamp, timezone.utc),
                    )
                else:
                    failures = [
                        (asset, check, "ERROR check failed; inspect restricted Dagster metadata.")
                        for asset, check, _ in failed_error_checks(instance, run.run_id)
                    ]
                    alerts = asset_check_payload(
                        run.job_name,
                        run.run_id,
                        failures,
                        now=datetime.fromtimestamp(entry.timestamp, timezone.utc),
                    )
                if not _enqueue(state, alerts, now):
                    break  # Do not acknowledge an event that could not be fully queued.
                state["streams"][stream] = record.storage_id
        except Exception as exc:
            state["source_error"] = type(exc).__name__


def _calendar_fires(conn, now):
    """Complete calendar, including closed days. Never infer holidays from missing bars."""
    ny = now.astimezone(_ET)
    end = (
        ny.date()
        if ny.time().replace(tzinfo=None) >= time(22, 30)
        else ny.date() - timedelta(days=1)
    )
    while end.weekday() >= 5:
        end -= timedelta(days=1)
    start = end - timedelta(days=14)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, is_trading_day FROM dw_stock.dim_trading_calendar "
            "WHERE trade_date BETWEEN %s AND %s ORDER BY trade_date",
            (start, end),
        )
        rows = cur.fetchall()
    days = dict(rows)
    if len(days) != len(rows):
        raise RuntimeError("calendar has duplicate dates")
    day = start
    while day <= end:
        if day.weekday() < 5 and (day not in days or type(days[day]) is not bool):
            raise RuntimeError("calendar incomplete")
        day += timedelta(days=1)
    return [
        datetime.combine(day, time(22, 30), tzinfo=_ET).astimezone(timezone.utc)
        for day, traded in sorted(days.items())
        if traded and day.weekday() < 5
    ]


def _schedule_alert(fire, now):
    alert = alertmanager_payload("research_trading_day", "unused", "", now=fire)[0]
    alert["labels"].pop("run_id")
    # A missing schedule is an incident, never a fictitious Dagster run.
    alert["labels"].update(
        alertname="BifrostDagsterScheduleMissed", session=fire.astimezone(_ET).date().isoformat()
    )
    alert["annotations"] = {
        "summary": "Research daily batch missed its completion deadline",
        "description": "No successful scheduled run within two hours of 22:30 ET.",
    }
    return alert


def _deadlines(instance, state, now, calendar_fires):
    state["deadline_error"] = None
    try:
        fires = calendar_fires(now)
        due = [fire for fire in fires if now >= fire + timedelta(hours=2)]
        if not due:
            raise RuntimeError("no measured deadline in calendar window")
        previous = state["last_fire"]
        if previous is not None and previous < fires[0].timestamp():
            raise RuntimeError(
                "deadline history exceeds 14-day window; operator reconciliation required"
            )
        # Bootstrap monitors only the latest due session, not a historical alert storm.
        new = [fire for fire in due if previous is not None and fire.timestamp() > previous]
        if previous is None:
            new = [due[-1]]
        targets = {str(fire.timestamp()): fire for fire in new}
        targets.update(
            {key: datetime.fromtimestamp(float(key), timezone.utc) for key in state["incidents"]}
        )
        for key, fire in sorted(targets.items(), key=lambda item: item[1]):
            records = instance.get_run_records(
                filters=RunsFilter(
                    job_name="research_trading_day",
                    created_after=fire,
                    created_before=fire + timedelta(hours=24),
                ),
                limit=101,
            )
            if len(records) > 100:
                raise RuntimeError("run deadline scan truncated")
            # Manual runs or old runs finishing late must not mask a missed schedule.
            scheduled = [
                r
                for r in records
                if r.dagster_run.tags.get("dagster/schedule_name")
                == "research_trading_day_schedule"
            ]
            recovered = any(r.dagster_run.status == DagsterRunStatus.SUCCESS for r in scheduled)
            alert = _schedule_alert(fire, now)
            if recovered and key in state["incidents"]:
                # Cancel an undelivered firing before adding its resolved identity.
                state["pending"].pop(_key(alert), None)
                if not _enqueue(state, [alert], now, resolved=True):
                    break
                del state["incidents"][key]
            elif not recovered and key not in state["incidents"]:
                if len(state["incidents"]) >= 64 or not _enqueue(state, [alert], now):
                    raise RuntimeError("deadline incident capacity reached")
                state["incidents"][key] = True
            if fire in new:
                state["last_fire"] = fire.timestamp()
    except Exception as exc:
        # Continue delivering the persisted outbox even if calendar/run reads fail.
        state["deadline_error"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__


def _read_calendar(now):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '20s'")
        return _calendar_fires(conn, now)
    finally:
        conn.rollback()
        conn.close()


@sensor(
    name=SENSOR_NAME,
    minimum_interval_seconds=60,
    default_status=DefaultSensorStatus.STOPPED,
    description="Durable failure delivery and daily deadline monitor; enable only after approved cutover.",
)
def bifrost_alert_delivery(context: SensorEvaluationContext):
    now = datetime.now(timezone.utc)
    state = _load(context.cursor, now)
    committed = set(state["pending"])
    # Reconcile recovery before sending obsolete firing alerts. New deadline
    # messages still wait until their cursor is committed by this tick.
    _deadlines(context.instance, state, now, _read_calendar)
    _deliver(state, now, committed)
    _discover(context.instance, state, now)
    state["checked_at"] = now.timestamp()
    context.update_cursor(json.dumps(state, sort_keys=True))
    return SkipReason(
        f"delivery pending={len(state['pending'])}; "
        f"source={state['source_error'] or 'measured'}; "
        f"deadline={state['deadline_error'] or 'measured'}; "
        "transport acknowledgment only; Owner receipt unverified"
    )
