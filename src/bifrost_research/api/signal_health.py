"""Signal Health aggregate — Wave 14.

GET /research/signal-health

Three kinds of "no reading" are kept apart (0.114.0). A table that does not
exist is ``missing``; a table that exists and holds nothing is ``empty``; a
probe that did not finish — the ``bifrost`` role's 2s ``statement_timeout``
cancelling a full scan of a 900 MB feature table while the database is busy —
is ``unprobed``, with its error and no row count. Until 0.114.0 every failure
read ``missing`` with 0 rows, which turned the console's asof amber over a
timeout and told the reader a 2.3M-row table was gone.

The whole payload is cached in-process for five minutes (thirty seconds when
any part of it failed): every page's asof tag and the sidebar read this
endpoint, and each computation scans the two largest feature tables several
times over.
"""

from __future__ import annotations

import logging
import threading
import time
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
        # None until the probe answers: a count nobody took is not zero.
        "row_count": None,
        "status": "unprobed",
        "age_hours": None,
        "sla_hours": freshness_sla_hours(),
    }
    try:
        with conn.cursor() as cur:
            # Catalogue lookups first — they cannot time out on the table's
            # size, and they are what tells "not there" from "not answered".
            cur.execute(
                """
                SELECT to_regclass(%s) IS NOT NULL,
                       EXISTS (
                           SELECT 1 FROM pg_attribute
                           WHERE attrelid = to_regclass(%s)
                             AND attname = 'computed_at'
                             AND NOT attisdropped
                       )
                """,
                (table, table),
            )
            exists, has_computed_at = cur.fetchone() or (False, False)
            if not exists:
                out["status"] = "missing"
                out["row_count"] = 0
                return out
            # One scan answers both. A failure here is a probe that did not
            # finish, never a reason to scan the table a second time.
            if has_computed_at:
                cur.execute(f"SELECT COUNT(*)::bigint, MAX(computed_at) FROM {table}")
                row = cur.fetchone() or (0, None)
            else:
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
        # Logged at info: a probe that did not finish is what a reader of the
        # console needs explained, and debug is where it used to disappear.
        logger.info("freshness probe did not finish for %s: %s", table, str(exc).splitlines()[0] if str(exc) else exc)
        try:
            conn.rollback()
        except Exception:
            pass
        out["status"] = "unprobed"
        out["row_count"] = None
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
    """Roll up table freshness rows — stale and missing both degrade.

    An ``unprobed`` row says nothing about the table, so it neither degrades
    the roll-up nor clears it; when no row answered at all the roll-up is
    ``unknown``.
    """
    if not freshness:
        return "empty"
    probed = [f for f in freshness if f.get("status") != "unprobed"]
    if not probed:
        return "unknown"
    if all(f.get("status") in ("empty", "missing") for f in probed):
        return "empty"
    if any(f.get("status") in ("missing", "stale") for f in probed):
        return "degraded"
    return "ok"


def _compute_signal_health() -> dict[str, Any]:
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
                    # Carried like iv_reconstruction's: a block that did not
                    # run and a block that found nothing both arrive as
                    # empties, and only this tells them apart.
                    **({"error": pnl_cov["error"]} if pnl_cov.get("error") else {}),
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


CACHE_TTL_SECONDS = 300.0
#: A payload with any part that did not finish is kept only briefly, so the
#: next reader retries rather than inheriting a timeout for five minutes.
PARTIAL_CACHE_TTL_SECONDS = 30.0

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "ttl": 0.0, "payload": None}


def _payload_is_partial(payload: dict[str, Any]) -> bool:
    data = payload.get("data") or {}
    rows = list(data.get("freshness") or []) + list(data.get("extra_tables") or [])
    if any(r.get("status") == "unprobed" for r in rows):
        return True
    blocks = (
        data.get("iv_reconstruction") or {},
        data.get("hypotheses") or {},
        data.get("canonical_pnl") or {},
    )
    return any(b.get("error") for b in blocks)


def _cached() -> dict[str, Any] | None:
    payload = _cache["payload"]
    if payload is not None and time.monotonic() - float(_cache["at"]) < float(_cache["ttl"]):
        return payload
    return None


def reset_cache() -> None:
    """Drop the cached payload (tests; an operator who wants a re-read restarts the pod)."""
    with _cache_lock:
        _cache.update(at=0.0, ttl=0.0, payload=None)


@router.get("")
def signal_health() -> dict[str, Any]:
    hit = _cached()
    if hit is not None:
        return hit
    # One computation at a time: readers that arrive while it runs wait for
    # its answer instead of starting their own scans.
    with _cache_lock:
        hit = _cached()
        if hit is not None:
            return hit
        payload = _compute_signal_health()
        ttl = PARTIAL_CACHE_TTL_SECONDS if _payload_is_partial(payload) else CACHE_TTL_SECONDS
        _cache.update(at=time.monotonic(), ttl=ttl, payload=payload)
        return payload


__all__ = [
    "FRESH_SLA_HOURS",
    "WEEKEND_SLA_HOURS",
    "_overall_from_freshness",
    "freshness_sla_hours",
    "freshness_status_from_age",
    "reset_cache",
    "router",
]
