"""Cron entrypoint — settle what happened to proposed candidates.

The Loop proposes symbols into ``research.candidate_pool`` and, until this
engine, nothing ever recorded how they did. "Is scan_legacy or stock_composite
better?" had no answer anywhere in the system, so every tuning change was a
matter of taste.

Forward legs come from ``raw_market.stock_daily`` at T+1 / T+5 / T+20 *sessions*
(not calendar days), the same convention as ``engines/signal_hit``. Horizons that
have not elapsed yet are skipped, never written as a zero.

D10 BLOCKED — reads prices, writes ``research.candidate_outcome``. No execution.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
from collections.abc import Sequence
from datetime import date
from typing import Any

from bifrost_research.db.conn import connect
from bifrost_research.db.upsert import batch_upsert
from bifrost_research.engines.candidate_outcome.build import (
    DEFAULT_BENCHMARK,
    DEFAULT_HORIZONS,
    excess_hit,
)
from bifrost_research.schema.schemas import (
    TABLE_RESEARCH_CANDIDATE_OUTCOME,
    TABLE_RESEARCH_CANDIDATE_POOL,
)

logger = logging.getLogger(__name__)

UPSERT_COLS = (
    "id",
    "candidate_id",
    "symbol",
    "trade_date",
    "horizon_days",
    "entry_close",
    "exit_close",
    "exit_date",
    "forward_return",
    "benchmark_symbol",
    "benchmark_return",
    "excess_return",
    "hit",
)

UPDATE_COLS = (
    "entry_close",
    "exit_close",
    "exit_date",
    "forward_return",
    "benchmark_symbol",
    "benchmark_return",
    "excess_return",
    "hit",
)


def outcome_id(candidate_id: str, horizon: int) -> str:
    """Deterministic id so re-running settles in place instead of duplicating."""
    digest = hashlib.sha1(f"{candidate_id}:{horizon}".encode()).hexdigest()[:16]
    return f"co_{digest}"


def _forward_leg(
    conn: Any, symbol: str, as_of: date, horizon: int
) -> tuple[float | None, float | None, date | None]:
    """Entry close, exit close and exit date `horizon` sessions after ``as_of``.

    A richer form of ``engines/signal_hit/entry.py::_fwd_return`` — that one
    needs only the scalar return, this one stores the legs it came from so a
    number on the page can be checked against real prices.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bar_date, close::float
            FROM raw_market.stock_daily
            WHERE symbol = %s
              AND bar_date >= %s
              AND close IS NOT NULL AND close > 0
            ORDER BY bar_date ASC
            LIMIT %s
            """,
            (symbol.upper(), as_of, horizon + 1),
        )
        rows = cur.fetchall() or []
    if len(rows) < horizon + 1:
        return None, None, None
    return float(rows[0][1]), float(rows[horizon][1]), rows[horizon][0]


def _close_on(conn: Any, symbol: str, day: date) -> float | None:
    """Close on an exact date — used to align the benchmark to the same window."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT close::float
            FROM raw_market.stock_daily
            WHERE symbol = %s AND bar_date = %s AND close IS NOT NULL AND close > 0
            """,
            (symbol.upper(), day),
        )
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _pending(conn: Any, *, lookback_days: int, horizons: Sequence[int]) -> list[dict[str, Any]]:
    """Candidates with at least one horizon not yet settled."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.id, c.symbol, c.trade_date,
                   COALESCE(array_agg(o.horizon_days) FILTER (WHERE o.horizon_days IS NOT NULL),
                            '{{}}') AS settled
            FROM {TABLE_RESEARCH_CANDIDATE_POOL} c
            LEFT JOIN {TABLE_RESEARCH_CANDIDATE_OUTCOME} o ON o.candidate_id = c.id
            WHERE c.trade_date >= CURRENT_DATE - %s::int
            GROUP BY c.id, c.symbol, c.trade_date
            ORDER BY c.trade_date DESC
            """,
            (lookback_days,),
        )
        rows = cur.fetchall() or []
    out: list[dict[str, Any]] = []
    for cid, symbol, trade_date, settled in rows:
        done = {int(h) for h in (settled or [])}
        missing = [h for h in horizons if h not in done]
        if missing:
            out.append(
                {
                    "id": cid,
                    "symbol": str(symbol or "").strip().upper(),
                    "trade_date": trade_date,
                    "horizons": missing,
                }
            )
    return out


def build_rows(
    conn: Any,
    *,
    lookback_days: int = 90,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    benchmark: str = DEFAULT_BENCHMARK,
) -> tuple[list[tuple], dict[str, int]]:
    rows: list[tuple] = []
    stats = {"candidates": 0, "settled": 0, "not_elapsed": 0, "no_price": 0, "no_benchmark": 0}

    for cand in _pending(conn, lookback_days=lookback_days, horizons=horizons):
        stats["candidates"] += 1
        for horizon in cand["horizons"]:
            entry, exit_close, exit_date = _forward_leg(
                conn, cand["symbol"], cand["trade_date"], horizon
            )
            if entry is None or exit_close is None or exit_date is None:
                # Either the window has not elapsed or the symbol has no bars.
                # Both mean "not yet known"; neither is a zero return.
                stats["not_elapsed"] += 1
                continue
            fwd = (exit_close / entry) - 1.0

            bench_entry = _close_on(conn, benchmark, cand["trade_date"])
            bench_exit = _close_on(conn, benchmark, exit_date)
            bench_ret = (
                (bench_exit / bench_entry) - 1.0
                if bench_entry and bench_exit and bench_entry > 0
                else None
            )
            if bench_ret is None:
                stats["no_benchmark"] += 1

            excess, hit = excess_hit(forward_return=fwd, benchmark_return=bench_ret)
            rows.append(
                (
                    outcome_id(cand["id"], horizon),
                    cand["id"],
                    cand["symbol"],
                    cand["trade_date"],
                    horizon,
                    entry,
                    exit_close,
                    exit_date,
                    fwd,
                    benchmark if bench_ret is not None else None,
                    bench_ret,
                    excess,
                    hit,
                )
            )
            stats["settled"] += 1
    return rows, stats


def run(
    *,
    lookback_days: int = 90,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    benchmark: str = DEFAULT_BENCHMARK,
) -> dict[str, Any]:
    conn = connect()
    try:
        rows, stats = build_rows(
            conn, lookback_days=lookback_days, horizons=horizons, benchmark=benchmark
        )
        if rows:
            batch_upsert(
                conn,
                TABLE_RESEARCH_CANDIDATE_OUTCOME,
                UPSERT_COLS,
                rows,
                conflict_keys=("candidate_id", "horizon_days"),
                update_cols=UPDATE_COLS,
                set_fetched_at=False,
            )
        return {
            "horizons": list(horizons),
            "benchmark": benchmark,
            "rows_written": len(rows),
            **stats,
        }
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    parser = argparse.ArgumentParser(description="Settle research.candidate_outcome")
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--horizons", type=str, default=",".join(str(h) for h in DEFAULT_HORIZONS))
    parser.add_argument("--benchmark", type=str, default=DEFAULT_BENCHMARK)
    args = parser.parse_args(list(argv) if argv is not None else None)
    horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())
    result = run(lookback_days=args.lookback_days, horizons=horizons, benchmark=args.benchmark)
    logger.info("candidate_outcome result=%s", result)
    print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
