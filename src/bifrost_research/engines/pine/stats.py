"""Pine signal statistics: the script's signal sessions, measured by ``engines.signal_stats`` (0.175.0).

Reads ``features.stock_signal_pine_daily`` across a rename; the method (next
open entry, cost, cooldown, cluster bootstrap, baseline, lineage) is
``engines/signal_stats.py``'s docstring. D10 BLOCKED — statistics only.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Sequence

from bifrost_research.engines.signal_stats import DEFAULT_COST_BPS, METHOD_VERSION, dedupe, evaluate, sample_note
from bifrost_research.repositories.listing_lineage import in_lineage, labels, live_label
from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_PINE_DAILY


def read_signals(
    conn: Any, script: str, side: str, start: date, end: date, symbols: Sequence[str]
) -> dict[str, list[date]]:
    """Signal sessions per company (live label), across a rename."""
    tickers = sorted({t for s in symbols for t in labels(s)})
    where_sym = "AND symbol = ANY(%(tickers)s::text[])" if tickers else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT DISTINCT UPPER(symbol), trade_date FROM {TABLE_STOCK_SIGNAL_PINE_DAILY}
                WHERE script_id = %(script)s AND side = %(side)s
                  AND trade_date BETWEEN %(start)s AND %(end)s {where_sym}""",
            {"script": script, "side": side, "start": start, "end": end, "tickers": tickers},
        )
        rows = cur.fetchall() or []
    out: dict[str, list[date]] = {}
    for sym, d in rows:
        d = d.date() if isinstance(d, datetime) else d
        if not in_lineage(conn, str(sym), d):
            continue
        out.setdefault(live_label(str(sym)), []).append(d)
    return {s: sorted(set(v)) for s, v in out.items()}


def signal_stats(
    conn: Any,
    *,
    script: str,
    side: str,
    symbols: Sequence[str],
    start: date,
    end: date,
    horizons: Sequence[int],
    move_threshold: float,
    cost_bps: float = DEFAULT_COST_BPS,
    today: date | None = None,
) -> dict[str, Any]:
    """The ``by_horizon`` block and its counts for ``GET /research/pine/signal-stats``."""
    return evaluate(
        conn,
        read_signals(conn, script, side, start, end, symbols),
        sign=1 if side == "buy" else -1,
        start=start,
        end=end,
        horizons=horizons,
        move_threshold=move_threshold,
        cost_bps=cost_bps,
        today=today,
    )


__all__ = ["DEFAULT_COST_BPS", "METHOD_VERSION", "dedupe", "read_signals", "sample_note", "signal_stats"]
