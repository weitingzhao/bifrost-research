"""Prometheus ``/metrics`` — scheduler liveness and Event Radar freshness (TD-99, TD-100).

The run-failure sensor reports a run that failed. Nothing reported a schedule
that stopped producing runs: STOPPED in the instance DB, renamed in code, a
daemon alive but not ticking, a tick that failed before launching a run, or a
run that hung. These series let Prometheus see each of those
(bifrost-trade-infra ``k8s/monitoring/bifrost-alerting-rules.yaml``, group
``bifrost-research-orchestration``).

Every liveness value is a timestamp; the rule computes the age. The "last due"
time comes from the code's cron (``schedule_roster``) at scrape time, so a
weekday-only or monthly schedule is never "late" on a day it is not due, and a
missed tick shows as ``last_due > last_tick``.

Read-only, fail-soft: a probe that errors sets ``bifrost_research_metrics_probe_ok``
to 0 for that probe and drops its series. D10 BLOCKED.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from bifrost_research.api.http_metrics import HTTP_METRICS
from bifrost_research.api.schedule_roster import SCHEDULE_ROSTER

logger = logging.getLogger(__name__)

router = APIRouter(tags=["metrics"])

# Dagster run statuses that are not finished.
_INFLIGHT = ("QUEUED", "NOT_STARTED", "STARTING", "STARTED", "CANCELING")

_HEARTBEATS_SQL = """
SELECT daemon_type, extract(epoch FROM "timestamp")
FROM ops_dagster.daemon_heartbeats
"""

# job_ticks.timestamp is the scheduled fire time (naive UTC). One LATERAL probe
# per instigator row rides idx_tick_selector_timestamp.
_SCHEDULES_SQL = """
SELECT i.instigator_body, i.status,
       extract(epoch FROM i.create_timestamp), extract(epoch FROM i.update_timestamp),
       t.tick_ts, t.tick_status
FROM ops_dagster.instigators i
LEFT JOIN LATERAL (
    SELECT extract(epoch FROM jt."timestamp") AS tick_ts, jt.status AS tick_status
    FROM ops_dagster.job_ticks jt
    WHERE jt.selector_id = i.selector_id AND jt.type = 'SCHEDULE'
    ORDER BY jt."timestamp" DESC
    LIMIT 1
) t ON true
WHERE i.instigator_type = 'SCHEDULE'
"""

_RUNS_SQL = """
SELECT pipeline_name,
       max(end_time) FILTER (WHERE status = 'SUCCESS'),
       min(coalesce(start_time, extract(epoch FROM create_timestamp)))
           FILTER (WHERE status = ANY(%(inflight)s))
FROM ops_dagster.runs
WHERE pipeline_name = ANY(%(jobs)s)
GROUP BY pipeline_name
"""

_EVENT_RADAR_SQL = """
SELECT
  (SELECT extract(epoch FROM max(fetched_at)) FROM raw_market.sec_8k_filing),
  (SELECT extract(epoch FROM max(computed_at))
     FROM features.event_signal_radar_daily WHERE source LIKE %(sec_source)s),
  (SELECT extract(epoch FROM max(computed_at)) FROM features.event_signal_radar_daily)
"""


def _esc(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class _Family:
    def __init__(self, name: str, help_text: str, kind: str = "gauge") -> None:
        self.name = name
        self.help = help_text
        self.kind = kind
        self.samples: list[tuple[dict[str, str], float]] = []

    def add(self, value: float | None, **labels: str) -> None:
        if value is None:
            return
        self.samples.append((labels, float(value)))

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        for labels, value in self.samples:
            if labels:
                inner = ",".join(f'{k}="{_esc(v)}"' for k, v in sorted(labels.items()))
                out.append(f"{self.name}{{{inner}}} {value:.17g}")
            else:
                out.append(f"{self.name} {value:.17g}")
        return out


def last_due_at(cron: str, tz: str, now: datetime) -> float | None:
    """Epoch seconds of the most recent fire time at or before ``now``."""
    try:
        from croniter import croniter
    except ImportError:  # image without the copilot extra
        return None
    zone = ZoneInfo(tz or "UTC")
    try:
        prev = croniter(cron, now.astimezone(zone)).get_prev(datetime)
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("croniter failed for %r: %s", cron, exc)
        return None
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=zone)
    return prev.timestamp()


def _schedule_name(body: Any) -> str | None:
    try:
        payload = json.loads(body) if isinstance(body, str) else (body or {})
    except (TypeError, json.JSONDecodeError):
        return None
    origin = payload.get("origin") if isinstance(payload, dict) else None
    if not isinstance(origin, dict):
        return None
    name = origin.get("job_name") or origin.get("instigator_name")
    return str(name) if name else None


def fold_instigator_rows(rows: Iterable[tuple[Any, ...]]) -> dict[str, dict[str, Any]]:
    """Per schedule name: state of the newest row, newest tick over all rows.

    A name can own several rows (research_trading_day_schedule has two selector
    ids since a code-location rename; one never ticked).
    """
    out: dict[str, dict[str, Any]] = {}
    for body, status, created, updated, tick_ts, tick_status in rows:
        name = _schedule_name(body)
        if name is None:
            continue
        cur = out.setdefault(
            name,
            {"status": None, "updated": -1.0, "created": None, "tick": None, "tick_status": None},
        )
        upd = float(updated or 0.0)
        if upd > cur["updated"]:
            cur["updated"] = upd
            cur["status"] = str(status or "")
        if created is not None:
            c = float(created)
            cur["created"] = c if cur["created"] is None else min(cur["created"], c)
        if tick_ts is not None and (cur["tick"] is None or float(tick_ts) > cur["tick"]):
            cur["tick"] = float(tick_ts)
            cur["tick_status"] = str(tick_status or "")
    return out


def _is_running(status: str | None) -> bool:
    # DefaultScheduleStatus.RUNNING is stored as DECLARED_IN_CODE until toggled.
    return (status or "").upper() in {"RUNNING", "DECLARED_IN_CODE"}


def build_schedule_families(
    instigators: dict[str, dict[str, Any]],
    runs: dict[str, tuple[float | None, float | None]],
    now: datetime,
) -> list[_Family]:
    running = _Family(
        "bifrost_dagster_schedule_running",
        "1 when the schedule is RUNNING in the Dagster instance; 0 when STOPPED or absent "
        "(state label: RUNNING, STOPPED or missing).",
    )
    due = _Family(
        "bifrost_dagster_schedule_last_due_timestamp_seconds",
        "Most recent fire time of the schedule's cron (code roster, its execution timezone).",
    )
    tick = _Family(
        "bifrost_dagster_schedule_last_tick_timestamp_seconds",
        "Scheduled time of the newest tick the daemon recorded; 0 when it never ticked.",
    )
    tick_status = _Family(
        "bifrost_dagster_schedule_last_tick_status",
        "1 for the status of the newest tick (SUCCESS, SKIPPED, FAILURE, STARTED).",
    )
    tracked = _Family(
        "bifrost_dagster_schedule_tracked_since_timestamp_seconds",
        "When the Dagster instance first recorded the schedule; a fire before this is not owed.",
    )
    success = _Family(
        "bifrost_dagster_job_last_success_timestamp_seconds",
        "End time of the job's newest SUCCESS run.",
    )
    inflight = _Family(
        "bifrost_dagster_job_oldest_inflight_start_timestamp_seconds",
        "Start (or create) time of the job's oldest unfinished run; absent when none.",
    )
    jobs_seen: set[str] = set()
    for spec in SCHEDULE_ROSTER:
        state = instigators.get(spec.name)
        status = (state or {}).get("status")
        label = "missing" if state is None else ("RUNNING" if _is_running(status) else str(status))
        running.add(1.0 if _is_running(status) else 0.0, schedule=spec.name, job=spec.job, state=label)
        due.add(last_due_at(spec.cron, spec.tz, now), schedule=spec.name, job=spec.job)
        last_tick = (state or {}).get("tick")
        tick.add(last_tick if last_tick is not None else 0.0, schedule=spec.name, job=spec.job)
        if state and state.get("tick_status"):
            tick_status.add(1.0, schedule=spec.name, job=spec.job, status=state["tick_status"])
        if state and state.get("created") is not None:
            tracked.add(state["created"], schedule=spec.name, job=spec.job)
        if spec.job in jobs_seen:
            continue
        jobs_seen.add(spec.job)
        last_ok, oldest_open = runs.get(spec.job, (None, None))
        success.add(last_ok, job=spec.job)
        inflight.add(oldest_open, job=spec.job)
    return [running, due, tick, tick_status, tracked, success, inflight]


def _probe(conn: Any, sql: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '5s'")
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.rollback()
    return rows


def collect(conn: Any, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    probe_ok = _Family(
        "bifrost_research_metrics_probe_ok",
        "1 when the probe's read succeeded on this scrape; its series are absent when 0.",
    )
    families: list[_Family] = [probe_ok]

    heart = _Family(
        "bifrost_dagster_daemon_heartbeat_timestamp_seconds",
        "Last heartbeat each Dagster daemon wrote (ops_dagster.daemon_heartbeats).",
    )
    try:
        for daemon_type, ts in _probe(conn, _HEARTBEATS_SQL):
            heart.add(ts, daemon_type=str(daemon_type))
        probe_ok.add(1.0, probe="daemon_heartbeats")
        families.append(heart)
    except Exception as exc:
        logger.warning("metrics: heartbeat probe failed: %s", exc)
        conn.rollback()
        probe_ok.add(0.0, probe="daemon_heartbeats")

    try:
        instigators = fold_instigator_rows(_probe(conn, _SCHEDULES_SQL))
        jobs = sorted({s.job for s in SCHEDULE_ROSTER})
        runs = {
            str(name): (ok, open_)
            for name, ok, open_ in _probe(
                conn, _RUNS_SQL, {"jobs": jobs, "inflight": list(_INFLIGHT)}
            )
        }
        families.extend(build_schedule_families(instigators, runs, now))
        probe_ok.add(1.0, probe="schedules")
    except Exception as exc:
        logger.warning("metrics: schedule probe failed: %s", exc)
        conn.rollback()
        probe_ok.add(0.0, probe="schedules")

    radar = _Family(
        "bifrost_research_event_radar_timestamp_seconds",
        "Event Radar freshness: sec_filing_fetched = newest raw_market.sec_8k_filing.fetched_at; "
        "sec_row_computed = newest SEC radar row; any_row_computed = newest radar row.",
    )
    try:
        params = {"sec_source": "ws:sec-8k-%"}
        for fetched, sec_row, any_row in _probe(conn, _EVENT_RADAR_SQL, params):
            radar.add(fetched, signal="sec_filing_fetched")
            radar.add(sec_row, signal="sec_row_computed")
            radar.add(any_row, signal="any_row_computed")
        probe_ok.add(1.0, probe="event_radar")
        families.append(radar)
    except Exception as exc:
        logger.warning("metrics: event radar probe failed: %s", exc)
        conn.rollback()
        probe_ok.add(0.0, probe="event_radar")

    lines: list[str] = []
    for fam in families:
        lines.extend(fam.render())
    return "\n".join(lines) + "\n"


@router.get("/metrics", response_class=PlainTextResponse)
def metrics() -> PlainTextResponse:
    media = "text/plain; version=0.0.4; charset=utf-8"
    try:
        from bifrost_research.db.conn import connect

        conn = connect()
    except Exception as exc:
        logger.warning("metrics: db connect failed: %s", exc)
        body = _Family(
            "bifrost_research_metrics_probe_ok",
            "1 when the probe's read succeeded on this scrape; its series are absent when 0.",
        )
        body.add(0.0, probe="connect")
        # The HTTP series do not need the database: a DB outage is when 5xx happen.
        text = "\n".join(body.render()) + "\n" + HTTP_METRICS.render()
        return PlainTextResponse(text, media_type=media)
    try:
        return PlainTextResponse(collect(conn) + HTTP_METRICS.render(), media_type=media)
    finally:
        try:
            conn.close()
        except Exception:
            pass


__all__ = ["router", "collect", "build_schedule_families", "fold_instigator_rows", "last_due_at"]
