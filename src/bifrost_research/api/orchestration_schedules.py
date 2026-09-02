"""Husbandry schedule whitelist + ops_dagster probes (fail-soft). D10 BLOCKED."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# (schedule_name, pipeline/job name in ops_dagster.runs)
HUSBANDRY_SCHEDULE_JOBS: tuple[tuple[str, str], ...] = (
    ("research_trading_day_schedule", "research_trading_day"),
    ("research_canonical_pnl_schedule", "research_canonical_pnl_job"),
    ("market_snapshot_schedule", "market_snapshot_job"),
    ("market_movers_schedule", "market_movers_job"),
    ("market_reference_schedule", "market_reference_job"),
    ("market_universe_calendar_schedule", "market_universe_calendar_job"),
    ("market_related_schedule", "market_related_job"),
    ("market_option_bars_schedule", "market_option_bars_job"),
    ("market_corporate_trades_schedule", "market_corporate_trades_job"),
    ("market_minute_bars_schedule", "market_minute_bars_job"),
    ("market_fundamentals_rotate_schedule", "market_fundamentals_rotate_job"),
    ("market_option_refresh_schedule", "market_option_refresh_job"),
    ("market_trim_schedule", "market_trim_job"),
    ("market_oi_gap_heal_schedule", "market_oi_gap_heal_job"),
    ("research_vrp_schedule", "research_vrp_job"),
    ("research_opex_schedule", "research_opex_job"),
    ("research_vol_surface_svi_schedule", "research_vol_surface_svi_job"),
    ("research_iv_solver_schedule", "research_iv_solver_job"),
    ("research_alert_scan_schedule", "research_alert_scan_job"),
    ("research_signal_hit_schedule", "research_signal_hit_job"),
    ("research_settlement_schedule", "research_settlement_job"),
    ("research_forecast_schedule", "research_forecast_job"),
    ("research_intraday_schedule", "research_intraday_job"),
    ("research_event_radar_schedule", "research_event_radar_job"),
    ("research_morning_prep_schedule", "research_morning_prep_job"),
    ("research_eod_review_schedule", "research_eod_review_job"),
    ("research_ensure_partitions_schedule", "research_ensure_partitions_job"),
    ("research_vol_weekly_backfill_schedule", "research_vol_weekly_backfill_job"),
)


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


def _is_permission_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "permission denied" in msg
        or "insufficientprivilege" in msg
        or getattr(exc, "pgcode", None) == "42501"
    )


def probe_schedule_states(conn: Any) -> dict[str, str]:
    """Map schedule_name → RUNNING|STOPPED|unknown from ops_dagster.instigators."""
    out: dict[str, str] = {}
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
                origin = payload.get("origin") if isinstance(payload, dict) else None
                name = None
                if isinstance(origin, dict):
                    name = origin.get("job_name") or origin.get("instigator_name")
                if not name or name in out:
                    continue
                out[str(name)] = _normalize_schedule_status(
                    str(status) if status is not None else payload.get("status")
                )
    except Exception as exc:
        if _is_permission_error(exc):
            try:
                conn.rollback()
            except Exception:
                pass
            return out
        logger.warning("probe_schedule_states failed: %s", exc)
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
    last_runs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pure builder for unit tests."""
    states = schedule_states or {}
    runs = last_runs or {}
    rows: list[dict[str, Any]] = []
    running = 0
    stopped = 0
    failures: list[dict[str, Any]] = []

    for sched_name, job_name in HUSBANDRY_SCHEDULE_JOBS:
        st = states.get(sched_name, "unknown")
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
            schedule_states=probe_schedule_states(conn),
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
    "probe_schedule_states",
    "probe_last_runs_by_job",
]
