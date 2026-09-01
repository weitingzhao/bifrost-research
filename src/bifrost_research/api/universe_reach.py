"""How much of the warehouse actually reaches the Loop.

GET /research/universe/reach

How far the Loop reaches depends on the universe_mode its active objectives
use.  ``scan_legacy`` proposes from ``features.stock_signal_scan_daily``, whose
universe is assembled from option-derived feature tables and is therefore
bounded by the option footprint — 28 symbols out of 14,836 with daily bars, or
1 in 530.  ``stock_composite`` reads SEPA instead and reaches 3,472.  Nothing in
the product said which was in play; you had to query the warehouse to find out.

Read-only.  D13: reads ``raw_market.*`` and ``features.*``, writes nothing.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter

from bifrost_research.db.conn import connect, rollback_quietly
from bifrost_research.schema.schemas import (
    TABLE_STOCK_SIGNAL_SCAN_DAILY,
    TABLE_STOCK_SIGNAL_SEPA_DAILY,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research/universe", tags=["research-universe"])

# Counts move once a day at most, and the whole sweep costs well under a second.
CACHE_TTL_SECONDS = 900.0

# Ordered widest → narrowest.  `narrows_to` is what the next layer can even see.
_LAYERS: tuple[dict[str, str], ...] = (
    {
        "key": "stock_daily",
        "label": "Daily bars",
        "table": "raw_market.stock_daily",
        "column": "symbol",
        "note": "Price history bought from the data vendor",
    },
    {
        "key": "stock_financials",
        "label": "Financials",
        "table": "raw_market.stock_financials",
        "column": "symbol",
        "note": "Filings — SEPA's fundamental layer needs these",
    },
    {
        "key": "sepa",
        "label": "SEPA features",
        "table": TABLE_STOCK_SIGNAL_SEPA_DAILY,
        "column": "symbol",
        "note": "Investable universe after liquidity and fundamentals",
    },
    {
        "key": "scan",
        "label": "Scan snapshot",
        "table": TABLE_STOCK_SIGNAL_SCAN_DAILY,
        "column": "symbol",
        "note": "What universe_mode=scan_legacy can propose from",
    },
    {
        "key": "option_daily",
        "label": "Option bars",
        "table": "raw_market.option_daily",
        "column": "underlying",
        "note": "Bounds the scan universe — its features are option-derived",
    },
)

_cache: dict[str, Any] | None = None
_cache_at: float = 0.0


def _count_symbols(conn: Any, table: str, column: str) -> int | None:
    """Distinct symbol count, or None when it cannot be measured.

    Never returns 0 on failure: a zero would render as "this layer is empty",
    which is a different and much more alarming claim than "we could not read it".
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(DISTINCT {column}) FROM {table}")
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("universe_reach: count failed for %s (%s)", table, exc)
        return None
    if not row or row[0] is None:
        return None
    return int(row[0])


# Which layer an objective's universe_mode actually draws from. Reporting the
# scan count as "what the Loop sees" was right only while scan_legacy was the
# only mode; once a stock_composite objective is active the Loop reaches the
# SEPA universe, and a strip still saying 28 would be exactly the kind of stale
# number this endpoint exists to prevent.
_MODE_LAYER: dict[str, str] = {
    "scan_legacy": "scan",
    "stock_composite": "sepa",
    "sepa": "sepa",
    "momentum": "sepa",
    "events": "sepa",
}


def _active_modes(conn: Any) -> list[str]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT COALESCE(policy_json ->> 'universe_mode', 'scan_legacy')
                FROM research.objective
                WHERE status = 'active'
                """
            )
            rows = cur.fetchall() or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("universe_reach: active modes unavailable (%s)", exc)
        rollback_quietly(conn)
        return []
    return sorted({str(r[0]) for r in rows if r and r[0]})


def build_reach(conn: Any) -> dict[str, Any]:
    """Layer counts plus the widest-to-Loop ratio."""
    layers: list[dict[str, Any]] = []
    for spec in _LAYERS:
        count = _count_symbols(conn, spec["table"], spec["column"])
        layers.append(
            {
                "key": spec["key"],
                "label": spec["label"],
                "table": spec["table"],
                "note": spec["note"],
                "symbols": count,
                "status": "ok" if count is not None else "unavailable",
            }
        )

    by_key = {layer["key"]: layer["symbols"] for layer in layers}
    widest = by_key.get("stock_daily")

    # The Loop reaches as far as its widest active universe_mode, not as far as
    # the scan table.
    modes = _active_modes(conn)
    reach_keys = {_MODE_LAYER.get(m, "scan") for m in modes} or {"scan"}
    candidates = [by_key.get(k) for k in reach_keys if by_key.get(k) is not None]
    loop = max(candidates) if candidates else None

    pct: float | None = None
    if widest and loop is not None and widest > 0:
        pct = round(100.0 * loop / widest, 2)

    return {
        "layers": layers,
        "widest_symbols": widest,
        "loop_symbols": loop,
        "loop_pct_of_widest": pct,
        "universe_modes": modes,
        "measured": all(layer["status"] == "ok" for layer in layers),
    }


@router.get("/reach")
def get_universe_reach(refresh: bool = False) -> dict[str, Any]:
    """Symbol counts at each layer between the warehouse and the Loop."""
    global _cache, _cache_at
    now = time.monotonic()
    if not refresh and _cache is not None and (now - _cache_at) < CACHE_TTL_SECONDS:
        return {"ok": True, "data": {**_cache, "cached": True}}

    conn = connect()
    try:
        data = build_reach(conn)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass

    _cache = data
    _cache_at = now
    return {"ok": True, "data": {**data, "cached": False}}
