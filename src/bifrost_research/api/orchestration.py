"""GET /research/orchestration/status — Dagster batch husbandry observe (fail-soft).

Reads Golden Source ``ops_dagster.runs`` when present. Never calls Dagster GraphQL.
D10 BLOCKED — read-only.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from bifrost_research.db.conn import connect

from bifrost_research.api.orchestration_schedules import attach_schedules_from_conn

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research/orchestration", tags=["research-orchestration"])

JOB_NAME = "research_trading_day"
_ET = ZoneInfo("America/New_York")
_SLA_TIME = time(22, 30)  # America/New_York schedule tick
_OVERDUE_GRACE = timedelta(hours=2)

ProbeKind = Literal[
    "ok",
    "schema_missing",
    "table_missing",
    "permission_denied",
    "empty",
    "error",
]


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _base(
    *,
    verdict: str,
    detail: str,
    last_run_status: str | None = None,
    last_run_ended_at: str | None = None,
    last_run_id: str | None = None,
    overdue: bool = False,
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "job_name": JOB_NAME,
        "last_run_status": last_run_status,
        "last_run_ended_at": last_run_ended_at,
        "last_run_id": last_run_id,
        "overdue": overdue,
        "detail": detail,
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _last_schedule_fire_et(now_et: datetime) -> datetime:
    """Most recent Mon–Fri 22:30 ET that has already occurred (or is happening)."""
    candidate = datetime.combine(now_et.date(), _SLA_TIME, tzinfo=_ET)
    if now_et < candidate:
        day = now_et.date() - timedelta(days=1)
    else:
        day = now_et.date()
    while day.weekday() >= 5:
        day = day - timedelta(days=1)
    return datetime.combine(day, _SLA_TIME, tzinfo=_ET)


def _past_sla_grace(now_et: datetime) -> bool:
    fire = _last_schedule_fire_et(now_et)
    return now_et >= fire + _OVERDUE_GRACE


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


def _status_rank(status: str | None) -> str:
    s = (status or "").upper()
    if s in {"SUCCESS", "SUCCESSFUL"}:
        return "success"
    if s in {"FAILURE", "FAILED", "CANCELED", "CANCELLED"}:
        return "failed"
    if s in {"STARTED", "STARTING", "QUEUED", "NOT_STARTED", "MANAGED"}:
        return "running"
    return "unknown"


def _is_permission_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "permission denied" in msg
        or "insufficientprivilege" in msg
        or getattr(exc, "pgcode", None) == "42501"
    )


def _probe_last_run(conn: Any) -> tuple[ProbeKind, dict[str, Any] | None, str | None]:
    """Return (kind, last_run|None, error_detail)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'ops_dagster')"
        )
        if not bool((cur.fetchone() or [False])[0]):
            return "schema_missing", None, None

        cur.execute(
            """
            SELECT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'ops_dagster' AND c.relname = 'runs' AND c.relkind = 'r'
            )
            """
        )
        # pg_class may hide relations without privileges on some setups; fall through to SELECT.
        table_visible = bool((cur.fetchone() or [False])[0])

        try:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'ops_dagster' AND table_name = 'runs'
                """
            )
            cols = {r[0] for r in cur.fetchall()}
        except Exception as exc:
            if _is_permission_error(exc):
                try:
                    conn.rollback()
                except Exception:
                    pass
                return "permission_denied", None, None
            raise

        if not cols:
            # Visible in pg_class but no column metadata usually means no SELECT —
            # or Dagster has not created the table yet. Probe with SELECT to grade.
            try:
                cur.execute("SELECT 1 FROM ops_dagster.runs LIMIT 1")
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if _is_permission_error(exc):
                    return "permission_denied", None, None
                msg = str(exc).lower()
                if "does not exist" in msg or "undefinedtable" in msg:
                    return "table_missing", None, None
                return "error", None, str(exc)
            # SELECT worked but information_schema empty — treat as empty probe path
            return "empty", None, None

        name_col = (
            "pipeline_name"
            if "pipeline_name" in cols
            else ("job_name" if "job_name" in cols else None)
        )
        if name_col is None or "status" not in cols or "run_id" not in cols:
            return "table_missing", None, "ops_dagster.runs missing expected columns"

        end_expr = (
            "update_timestamp"
            if "update_timestamp" in cols
            else ("end_time" if "end_time" in cols else "create_timestamp")
        )
        order_expr = (
            "create_timestamp"
            if "create_timestamp" in cols
            else ("id" if "id" in cols else "run_id")
        )

        try:
            cur.execute(
                f"""
                SELECT run_id, status, {end_expr}
                FROM ops_dagster.runs
                WHERE {name_col} = %s
                ORDER BY {order_expr} DESC NULLS LAST
                LIMIT 1
                """,
                (JOB_NAME,),
            )
            got = cur.fetchone()
            if not got and "run_body" in cols:
                cur.execute(
                    f"""
                    SELECT run_id, status, {end_expr}
                    FROM ops_dagster.runs
                    WHERE run_body::text LIKE %s
                    ORDER BY {order_expr} DESC NULLS LAST
                    LIMIT 1
                    """,
                    (f"%{JOB_NAME}%",),
                )
                got = cur.fetchone()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if _is_permission_error(exc):
                return "permission_denied", None, None
            return "error", None, str(exc)

        if not got:
            return "empty", None, None
        return (
            "ok",
            {
                "run_id": str(got[0]),
                "status": str(got[1]) if got[1] is not None else None,
                "ended_at": _parse_ts(got[2]),
            },
            None,
        )


def compute_orchestration_status(
    *,
    now: datetime | None = None,
    last_run: dict[str, Any] | None = None,
    schema_missing: bool = False,
    table_missing: bool = False,
    permission_denied: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    """Pure verdict helper (unit-testable)."""
    if error:
        return _base(verdict="caution", detail=f"orchestration probe error: {error}")
    if schema_missing:
        return _base(verdict="unknown", detail="ops_dagster schema missing")
    if permission_denied:
        return _base(
            verdict="caution",
            detail="ops_dagster.runs permission denied",
        )
    if table_missing:
        return _base(
            verdict="unknown",
            detail="ops_dagster.runs table missing — Dagster may not have started",
        )

    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_et = now_utc.astimezone(_ET)
    fire = _last_schedule_fire_et(now_et)
    past_grace = _past_sla_grace(now_et)

    if last_run is None:
        if past_grace:
            return _base(
                verdict="missed",
                detail="no research_trading_day runs after weekday 22:30 ET SLA",
                overdue=True,
            )
        return _base(
            verdict="caution",
            detail="no research_trading_day runs recorded (within SLA grace)",
            overdue=False,
        )

    status_raw = last_run.get("status")
    kind = _status_rank(str(status_raw) if status_raw is not None else None)
    ended = last_run.get("ended_at")
    ended_iso = None
    if isinstance(ended, datetime):
        ended_iso = ended.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    run_id = last_run.get("run_id")
    run_id_s = str(run_id) if run_id is not None else None

    if kind == "failed":
        return _base(
            verdict="degraded",
            detail=f"last run {status_raw}",
            last_run_status=str(status_raw) if status_raw else None,
            last_run_ended_at=ended_iso,
            last_run_id=run_id_s,
            overdue=False,
        )

    if kind == "running":
        return _base(
            verdict="due",
            detail=f"run in progress ({status_raw})",
            last_run_status=str(status_raw) if status_raw else None,
            last_run_ended_at=ended_iso,
            last_run_id=run_id_s,
            overdue=False,
        )

    if kind == "success":
        ended_et = ended.astimezone(_ET) if isinstance(ended, datetime) else None
        covered = ended_et is not None and ended_et >= fire
        if past_grace and not covered:
            return _base(
                verdict="missed",
                detail="last success older than weekday 22:30 ET SLA",
                last_run_status=str(status_raw) if status_raw else None,
                last_run_ended_at=ended_iso,
                last_run_id=run_id_s,
                overdue=True,
            )
        return _base(
            verdict="healthy",
            detail="last research_trading_day success within SLA",
            last_run_status=str(status_raw) if status_raw else None,
            last_run_ended_at=ended_iso,
            last_run_id=run_id_s,
            overdue=False,
        )

    return _base(
        verdict="caution",
        detail=f"unrecognized run status={status_raw}",
        last_run_status=str(status_raw) if status_raw else None,
        last_run_ended_at=ended_iso,
        last_run_id=run_id_s,
    )


@router.get("/status")
def orchestration_status() -> dict[str, Any]:
    try:
        conn = connect()
    except Exception as exc:
        logger.warning("orchestration status: db connect failed: %s", exc)
        return _ok(compute_orchestration_status(error=f"database unavailable: {exc}"))

    try:
        try:
            kind, last, err = _probe_last_run(conn)
            if kind == "schema_missing":
                data = compute_orchestration_status(schema_missing=True)
            elif kind == "permission_denied":
                data = compute_orchestration_status(permission_denied=True)
            elif kind == "table_missing":
                data = compute_orchestration_status(table_missing=True)
            elif kind == "error":
                data = compute_orchestration_status(error=err or "probe failed")
            elif kind == "empty":
                data = compute_orchestration_status(last_run=None)
            else:
                data = compute_orchestration_status(last_run=last)
            data = attach_schedules_from_conn(conn, data)
        except Exception as exc:
            logger.warning("orchestration status probe failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
            if _is_permission_error(exc):
                data = compute_orchestration_status(permission_denied=True)
            else:
                data = compute_orchestration_status(error=str(exc))
            data = attach_schedules_from_conn(conn, data)
        return _ok(data)
    finally:
        try:
            conn.close()
        except Exception:
            pass


__all__ = [
    "router",
    "compute_orchestration_status",
    "JOB_NAME",
]
