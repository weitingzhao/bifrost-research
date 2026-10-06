"""Husbandry schedule whitelist + ops_dagster probes (fail-soft). D10 BLOCKED."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from bifrost_research.api.schedule_roster import (
    HUSBANDRY_SCHEDULE_JOBS,
    ROSTER_BY_NAME,
)

logger = logging.getLogger(__name__)


def _normalize_schedule_status(raw: str | None) -> str:
    s = (raw or "").upper()
    if s == "RUNNING":
        return "RUNNING"
    if s == "STOPPED":
        return "STOPPED"
    # DefaultScheduleStatus.RUNNING materializes as DECLARED_IN_CODE until toggled.
    if s == "DECLARED_IN_CODE":
        return "RUNNING"
    return "unknown"


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _cron_from_body(payload: dict[str, Any]) -> str | None:
    """Dagster stores the cron under job_specific_data.cron_schedule."""
    specific = payload.get("job_specific_data")
    if not isinstance(specific, dict):
        return None
    cron = specific.get("cron_schedule")
    if isinstance(cron, str) and cron.strip():
        return cron.strip()
    return None


def next_tick_at(
    cron: str | None,
    *,
    status: str,
    now: datetime | None = None,
    tz: str = "UTC",
) -> str | None:
    """Next fire time in UTC ISO-Z when the schedule is RUNNING; else None.

    ``tz`` is the ScheduleDefinition execution_timezone. Cron fields are wall
    clock in that zone; the return value is always UTC. STOPPED schedules must
    not promise a next run. Missing croniter (image without the copilot extra)
    fails soft to None.
    """
    if status != "RUNNING":
        return None
    expr = (cron or "").strip()
    if not expr:
        return None
    try:
        from croniter import croniter
    except ImportError:
        logger.debug("croniter not installed — next_tick_at unavailable")
        return None
    try:
        zone = ZoneInfo(tz) if tz else timezone.utc
    except (KeyError, ValueError):
        zone = timezone.utc
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    base = base.astimezone(zone)
    try:
        nxt = croniter(expr, base).get_next(datetime)
    except (ValueError, KeyError, TypeError) as exc:
        logger.debug("croniter failed for %r: %s", expr, exc)
        return None
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=zone)
    return _iso(nxt)


def _is_permission_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "permission denied" in msg
        or "insufficientprivilege" in msg
        or getattr(exc, "pgcode", None) == "42501"
    )


def probe_schedule_states(conn: Any) -> dict[str, str]:
    """Map schedule_name → RUNNING|STOPPED|unknown from ops_dagster.instigators."""
    return {name: meta["status"] for name, meta in probe_schedule_meta(conn).items()}


def probe_schedule_meta(conn: Any) -> dict[str, dict[str, Any]]:
    """Map schedule_name → {status, cron_schedule} from ops_dagster.instigators."""
    out: dict[str, dict[str, Any]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM pg_catalog.pg_class c
                  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                  WHERE n.nspname = 'ops_dagster'
                    AND c.relname = 'instigators'
                    AND c.relkind = 'r'
                )
                """
            )
            if not bool((cur.fetchone() or [False])[0]):
                return out
            cur.execute(
                """
                SELECT status, instigator_body, update_timestamp
                FROM ops_dagster.instigators
                WHERE instigator_type = 'SCHEDULE'
                ORDER BY update_timestamp DESC NULLS LAST
                """
            )
            for status, body, _upd in cur.fetchall():
                try:
                    payload = json.loads(body) if isinstance(body, str) else (body or {})
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                origin = payload.get("origin")
                name = None
                if isinstance(origin, dict):
                    name = origin.get("job_name") or origin.get("instigator_name")
                if not name or name in out:
                    continue
                out[str(name)] = {
                    "status": _normalize_schedule_status(
                        str(status) if status is not None else payload.get("status")
                    ),
                    "cron_schedule": _cron_from_body(payload),
                }
    except Exception as exc:
        if _is_permission_error(exc):
            try:
                conn.rollback()
            except Exception:
                pass
            return out
        logger.warning("probe_schedule_meta failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
    return out


def probe_last_runs_by_job(conn: Any) -> dict[str, dict[str, Any]]:
    """Map pipeline_name → {run_id, status, ended_at} for latest run each."""
    out: dict[str, dict[str, Any]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (pipeline_name)
                  pipeline_name, run_id, status, update_timestamp, end_time
                FROM ops_dagster.runs
                WHERE pipeline_name IS NOT NULL
                ORDER BY pipeline_name, create_timestamp DESC NULLS LAST
                """
            )
            for row in cur.fetchall():
                pipe, run_id, status, upd, end = row
                ended = _parse_ts(end) or _parse_ts(upd)
                out[str(pipe)] = {
                    "run_id": str(run_id) if run_id is not None else None,
                    "status": str(status) if status is not None else None,
                    "ended_at": ended,
                }
    except Exception as exc:
        if _is_permission_error(exc):
            try:
                conn.rollback()
            except Exception:
                pass
            return out
        logger.warning("probe_last_runs_by_job failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
    return out


def build_schedules_summary(
    *,
    schedule_states: dict[str, str] | None = None,
    schedule_meta: dict[str, dict[str, Any]] | None = None,
    last_runs: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure builder for unit tests."""
    meta = dict(schedule_meta or {})
    if schedule_states:
        for name, st in schedule_states.items():
            bucket = meta.setdefault(name, {})
            bucket.setdefault("status", st)
    runs = last_runs or {}
    rows: list[dict[str, Any]] = []
    running = 0
    stopped = 0
    failures: list[dict[str, Any]] = []

    for sched_name, job_name, tz in HUSBANDRY_SCHEDULE_JOBS:
        info = meta.get(sched_name) or {}
        st = str(info.get("status") or "unknown")
        spec = ROSTER_BY_NAME[sched_name]
        # The code's cron, not the instigator row's: a DECLARED_IN_CODE row keeps
        # the cron it was created with. Measured 2026-10-06: ratios, treasury and
        # research_intraday rows still held crons replaced weeks earlier, while
        # the daemon ticked on the code's (ratios fired 14:10, the row said 2-20/3).
        cron_s = spec.cron
        if st == "RUNNING":
            running += 1
        elif st == "STOPPED":
            stopped += 1
        last = runs.get(job_name)
        last_status = last.get("status") if last else None
        last_ended = _iso(last.get("ended_at")) if last else None
        last_id = last.get("run_id") if last else None
        row = {
            "name": sched_name,
            "job_name": job_name,
            "status": st,
            "cron_schedule": cron_s,
            "execution_timezone": tz,
            # Plugin slots this schedule fires; Console maps slot -> schedule from
            # these instead of keeping its own table (TD-108).
            "market_slots": list(spec.market_slots),
            "next_tick_at": next_tick_at(cron_s, status=st, now=now, tz=tz),
            "last_run_status": last_status,
            "last_run_ended_at": last_ended,
            "last_run_id": last_id,
        }
        rows.append(row)
        if last_status and str(last_status).upper() in {
            "FAILURE",
            "FAILED",
            "CANCELED",
            "CANCELLED",
        }:
            failures.append(
                {
                    "name": sched_name,
                    "job_name": job_name,
                    "last_run_status": last_status,
                    "last_run_ended_at": last_ended,
                    "last_run_id": last_id,
                }
            )

    failures = failures[:3]
    return {
        "schedules": rows,
        "schedules_total": len(rows),
        "schedules_running": running,
        "schedules_stopped": stopped,
        "schedules_unknown": len(rows) - running - stopped,
        "recent_failures": failures,
    }


def attach_schedules_from_conn(conn: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Mutate status payload with multi-schedule summary (fail-soft)."""
    try:
        summary = build_schedules_summary(
            schedule_meta=probe_schedule_meta(conn),
            last_runs=probe_last_runs_by_job(conn),
        )
        data.update(summary)
    except Exception as exc:
        logger.warning("attach_schedules_from_conn: %s", exc)
        data.setdefault("schedules", [])
        data.setdefault("schedules_total", len(HUSBANDRY_SCHEDULE_JOBS))
        data.setdefault("schedules_running", 0)
        data.setdefault("schedules_stopped", 0)
        data.setdefault("schedules_unknown", len(HUSBANDRY_SCHEDULE_JOBS))
        data.setdefault("recent_failures", [])
        data["schedules_detail"] = f"schedules probe failed: {exc}"
    return data


__all__ = [
    "HUSBANDRY_SCHEDULE_JOBS",
    "build_schedules_summary",
    "attach_schedules_from_conn",
    "next_tick_at",
    "probe_schedule_meta",
    "probe_schedule_states",
    "probe_last_runs_by_job",
]
