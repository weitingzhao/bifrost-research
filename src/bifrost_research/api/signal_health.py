"""Signal Health aggregate — Wave 14.

GET /research/signal-health
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from bifrost_research.db.conn import connect
from bifrost_research.engines.canonical_pnl import coverage_report
from bifrost_research.schema.schemas import (
    CANONICAL_FEATURE_TABLES,
    TABLE_OPTION_IV_RECONSTRUCTED_DAILY,
    TABLE_RESEARCH_HYPOTHESIS,
    TABLE_STOCK_BACKTEST_SETTLEMENT,
    TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY,
    TABLE_STOCK_SIGNAL_PLAYBOOK_TRIGGER_INTRADAY,
    TABLE_STOCK_SIGNAL_SCAN_DAILY,
    TABLE_STOCK_SIGNAL_VRP_DAILY,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research/signal-health", tags=["research-signal-health"])

# Align with Massive plugin weekend freshness (Sat/Sun/Mon-before-22:00 UTC → 72h).
FRESH_SLA_HOURS = 36.0
WEEKEND_SLA_HOURS = 72.0


def freshness_sla_hours(now: datetime | None = None) -> float:
    """Mon–Fri batch + 36h SLA false-trips Monday afternoon without a weekend window."""
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    weekday = ts.weekday()  # Mon=0 … Sun=6
    if weekday >= 5 or (weekday == 0 and ts.hour < 22):
        return WEEKEND_SLA_HOURS
    return FRESH_SLA_HOURS


def freshness_status_from_age(age: float, *, now: datetime | None = None) -> str:
    return "fresh" if age <= freshness_sla_hours(now) else "stale"

# Core tables for cron freshness (subset of feature store)
_FRESHNESS_TABLES: tuple[tuple[str, str], ...] = (
    ("vrp", TABLE_STOCK_SIGNAL_VRP_DAILY),
    ("canonical_pnl", TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY),
    ("iv_reconstructed", TABLE_OPTION_IV_RECONSTRUCTED_DAILY),
    ("playbook_trigger", TABLE_STOCK_SIGNAL_PLAYBOOK_TRIGGER_INTRADAY),
    ("scan", TABLE_STOCK_SIGNAL_SCAN_DAILY),
    ("forecast_settlement", TABLE_STOCK_BACKTEST_SETTLEMENT),
)


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


def _table_freshness(conn: Any, label: str, table: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "label": label,
        "table": table,
        "max_computed_at": None,
        "row_count": 0,
        "status": "missing",
        "age_hours": None,
        "sla_hours": freshness_sla_hours(),
    }
    try:
        with conn.cursor() as cur:
            # Prefer computed_at; fall back to as_of_date / trade_date presence
            try:
                cur.execute(
                    f"""
                    SELECT COUNT(*)::bigint,
                           MAX(computed_at)
                    FROM {table}
                    """
                )
                row = cur.fetchone() or (0, None)
            except Exception:
                conn.rollback()
                cur.execute(f"SELECT COUNT(*)::bigint FROM {table}")
                cnt = cur.fetchone()
                row = (cnt[0] if cnt else 0, None)
        count = int(row[0] or 0)
        max_ts = row[1]
        out["row_count"] = count
        if max_ts is not None:
            if isinstance(max_ts, datetime):
                iso = max_ts.isoformat()
                if max_ts.tzinfo is None:
                    age = (datetime.utcnow() - max_ts).total_seconds() / 3600.0
                else:
                    age = (datetime.now(timezone.utc) - max_ts.astimezone(timezone.utc)).total_seconds() / 3600.0
            else:
                iso = str(max_ts)
                age = None
            out["max_computed_at"] = iso
            out["age_hours"] = age
            sla = freshness_sla_hours()
            out["sla_hours"] = sla
            if age is None:
                out["status"] = "unknown"
            else:
                out["status"] = freshness_status_from_age(age)
        elif count > 0:
            out["status"] = "unknown"
        else:
            out["status"] = "empty"
    except Exception as exc:
        logger.debug("freshness failed for %s: %s", table, exc)
        try:
            conn.rollback()
        except Exception:
            pass
        out["status"] = "missing"
        out["error"] = str(exc)
    return out


def _hypothesis_counts(conn: Any) -> dict[str, Any]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT status, COUNT(*)::bigint
                FROM {TABLE_RESEARCH_HYPOTHESIS}
                WHERE retired_at IS NULL
                GROUP BY status
                """
            )
            rows = cur.fetchall() or []
        counts = {str(r[0]): int(r[1]) for r in rows}
        return {
            "counts": counts,
            "total_active": int(counts.get("active") or 0),
            "total": sum(counts.values()),
        }
    except Exception as exc:
        logger.debug("hypothesis counts unavailable: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"counts": {}, "total_active": 0, "total": 0, "error": str(exc)}


def _iv_reconstruction(conn: Any) -> dict[str, Any]:
    """Coverage for features.option_iv_reconstructed_daily (IDS-5)."""
    iv_coverage_report = None
    try:
        from bifrost_research.engines.volatility.iv_solver import (
            coverage_report as _iv_cov,
        )

        iv_coverage_report = _iv_cov
    except ImportError:
        pass

    if iv_coverage_report is not None:
        try:
            return iv_coverage_report(conn)
        except Exception as exc:
            logger.debug("iv_solver.coverage_report unavailable: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
            return {
                "rows": 0,
                "symbols": 0,
                "distinct_dates": 0,
                "with_iv": 0,
                "by_status": {},
                "solver_ok_pct": None,
                "error": str(exc),
            }

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)::bigint,
                       COUNT(DISTINCT symbol)::bigint,
                       COUNT(DISTINCT trade_date)::bigint
                FROM {TABLE_OPTION_IV_RECONSTRUCTED_DAILY}
                """
            )
            row = cur.fetchone() or (0, 0, 0)
            cur.execute(
                f"""
                SELECT solver_status, COUNT(*)::bigint
                FROM {TABLE_OPTION_IV_RECONSTRUCTED_DAILY}
                GROUP BY 1
                """
            )
            by_status = {str(r[0]): int(r[1]) for r in cur.fetchall()}
            cur.execute(
                f"""
                SELECT COUNT(*)::bigint
                FROM {TABLE_OPTION_IV_RECONSTRUCTED_DAILY}
                WHERE iv IS NOT NULL
                """
            )
            with_iv = int((cur.fetchone() or (0,))[0] or 0)
        total = int(row[0] or 0)
        ok = int(by_status.get("ok") or 0) + int(by_status.get("vendor_snapshot") or 0)
        return {
            "rows": total,
            "symbols": int(row[1] or 0),
            "distinct_dates": int(row[2] or 0),
            "with_iv": with_iv,
            "by_status": by_status,
            "solver_ok_pct": (ok / total) if total else None,
        }
    except Exception as exc:
        logger.debug("iv_reconstruction unavailable: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return {
            "rows": 0,
            "symbols": 0,
            "distinct_dates": 0,
            "with_iv": 0,
            "by_status": {},
            "solver_ok_pct": None,
            "error": str(exc),
        }


def _overall_from_freshness(freshness: list[dict[str, Any]]) -> str:
    """Roll up table freshness rows — stale and missing both degrade."""
    if not freshness:
        return "empty"
    if all(f.get("status") in ("empty", "missing") for f in freshness):
        return "empty"
    if any(f.get("status") in ("missing", "stale") for f in freshness):
        return "degraded"
    return "ok"


@router.get("")
def signal_health() -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        freshness = [_table_freshness(conn, label, table) for label, table in _FRESHNESS_TABLES]
        # Sample a few more feature tables (best-effort)
        extra: list[dict[str, Any]] = []
        for table in list(CANONICAL_FEATURE_TABLES)[:6]:
            short = table.split(".")[-1]
            if short in {t.split(".")[-1] for _, t in _FRESHNESS_TABLES}:
                continue
            extra.append(_table_freshness(conn, short, table))

        hyp = _hypothesis_counts(conn)
        pnl_cov: dict[str, Any] = {}
        try:
            pnl_cov = coverage_report(conn)
        except Exception as exc:
            logger.debug("canonical_pnl coverage unavailable: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
            pnl_cov = {"insufficient_pct": None, "error": str(exc)}

        iv_recon = _iv_reconstruction(conn)

        overall = _overall_from_freshness(freshness)

        return _ok(
            {
                "overall": overall,
                "as_of": datetime.utcnow().isoformat() + "Z",
                "sla_hours": freshness_sla_hours(),
                "freshness": freshness,
                "extra_tables": extra,
                "hypotheses": hyp,
                "canonical_pnl": {
                    "insufficient_pct": pnl_cov.get("insufficient_pct"),
                    "rows": pnl_cov.get("rows"),
                    "symbols": pnl_cov.get("symbols"),
                    "by_quality": pnl_cov.get("by_quality"),
                },
                "iv_reconstruction": iv_recon,
            }
        )
    except Exception as exc:
        logger.exception("signal-health failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


__all__ = [
    "router",
    "_overall_from_freshness",
    "freshness_sla_hours",
    "freshness_status_from_age",
    "FRESH_SLA_HOURS",
    "WEEKEND_SLA_HOURS",
]
