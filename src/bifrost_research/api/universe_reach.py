"""How much of the warehouse actually reaches the Loop.

GET /research/universe/reach

The Loop's candidate proposals come from ``features.stock_signal_scan_daily``,
whose universe is assembled from option-derived feature tables — so it is bounded
by the option data footprint, not by how many symbols were bought.  Measured
2026-09-01: 14,836 symbols have daily bars and 28 reach the scan, which is 1 in
530.  Nothing in the product said so; you had to query the warehouse to find out.

Read-only.  D13: reads ``raw_market.*`` and ``features.*``, writes nothing.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter

from bifrost_research.db.conn import connect
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
    loop = by_key.get("scan")
    pct: float | None = None
    if widest and loop is not None and widest > 0:
        pct = round(100.0 * loop / widest, 2)

    return {
        "layers": layers,
        "widest_symbols": widest,
        "loop_symbols": loop,
        "loop_pct_of_widest": pct,
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
