"""Track record — how a lens' triggers actually settled, from stock_signal_lens_hit_daily.

Symbol-scoped when the symbol has enough triggers in the window, otherwise the
lens' record across symbols, and the answer says which. The window defaults to
90 days so the 20-session horizon has settled rows to report.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_LENS_HIT_DAILY

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 90
MIN_SYMBOL_ROWS = 10


def _lens_hit_rows(conn: Any, decay_lens: str, cutoff: date, symbol: str | None) -> list[tuple[Any, ...]]:
    params: list[Any] = [decay_lens, cutoff]
    where = "lens = %s AND trade_date >= %s"
    if symbol:
        where += " AND symbol = %s"
        params.append(symbol)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT trigger_side, hit_5d, hit_20d
            FROM {TABLE_STOCK_SIGNAL_LENS_HIT_DAILY}
            WHERE {where}
            """,
            params,
        )
        return list(cur.fetchall() or [])


def _side_hits(rows: list[tuple[Any, ...]], side: str | None) -> dict[str, Any]:
    subset = [r for r in rows if side is None or r[0] == side]
    eval5 = [r for r in subset if r[1] is not None]
    eval20 = [r for r in subset if r[2] is not None]
    hit5 = sum(1 for r in eval5 if r[1])
    hit20 = sum(1 for r in eval20 if r[2])
    return {
        "n": len(subset),
        "evaluated_5d": len(eval5),
        "hit_rate_5d": round(hit5 / len(eval5), 4) if eval5 else None,
        "evaluated_20d": len(eval20),
        "hit_rate_20d": round(hit20 / len(eval20), 4) if eval20 else None,
    }


def fetch_track_record(
    conn: Any,
    decay_lens: str,
    symbol: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_symbol_rows: int = MIN_SYMBOL_ROWS,
) -> dict[str, Any] | None:
    """Settled hit rates for the lens; None when the table has nothing in the window."""
    cutoff = date.today() - timedelta(days=window_days)
    try:
        rows = _lens_hit_rows(conn, decay_lens, cutoff, symbol)
        scoped = len(rows) >= min_symbol_rows
        if not scoped:
            rows = _lens_hit_rows(conn, decay_lens, cutoff, None)
    except Exception as exc:
        logger.debug("track record unavailable for %s %s: %s", decay_lens, symbol, exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    if not rows:
        return None
    overall = _side_hits(rows, None)
    return {
        "lens": decay_lens,
        "window_days": window_days,
        "symbol_scoped": scoped,
        "n": overall["n"],
        "hit_rate_5d": overall["hit_rate_5d"],
        "hit_rate_20d": overall["hit_rate_20d"],
        "by_side": {"hot": _side_hits(rows, "hot"), "cold": _side_hits(rows, "cold")},
    }
