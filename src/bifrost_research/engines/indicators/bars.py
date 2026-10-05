"""Daily closes from ``raw_market.stock_daily`` for indicator work (read-only)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping

from bifrost_research.repositories.listing_lineage import stock_clause


def load_bars(
    conn: Any, symbol: str, start: date, end: date, *, warmup_sessions: int = 0
) -> list[dict[str, Any]]:
    """Adjusted daily bars in [start - warmup, end], oldest first.

    Warm-up is taken in calendar days (sessions × 1.5 + 10) so the first
    session in ``start`` already has a settled indicator value.
    """
    lo = start - timedelta(days=int(warmup_sessions * 1.5) + 10) if warmup_sessions else start
    clause, params = stock_clause(conn, symbol)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT bar_date, open, high, low, close, volume
            FROM raw_market.stock_daily
            WHERE {clause}
              AND bar_date BETWEEN %s AND %s
              AND close IS NOT NULL
            ORDER BY bar_date
            """,
            (*params, lo, end),
        )
        rows = cur.fetchall() or []
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, Mapping):
            d, o, h, low, c, v = (r["bar_date"], r["open"], r["high"], r["low"], r["close"], r["volume"])
        else:
            d, o, h, low, c, v = r
        if isinstance(d, datetime):
            d = d.date()
        out.append(
            {
                "date": d,
                "open": float(o) if o is not None else None,
                "high": float(h) if h is not None else None,
                "low": float(low) if low is not None else None,
                "close": float(c),
                "volume": float(v) if v is not None else None,
            }
        )
    return out
