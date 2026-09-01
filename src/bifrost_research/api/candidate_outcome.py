"""What happened to the candidates the Loop proposed.

GET /research/candidate-outcome/summary   — hit rate per horizon, sliceable by source
GET /research/candidate-outcome/rows      — settled legs, newest first

`hit` is "beat SPY over the same window", not "went up": candidates carry no
direction, and an absolute win rate mostly measures the market. Horizons that
have not elapsed are absent rather than zero, so `pending` is reported next to
`settled` — an empty ledger on a young pool means "not known yet", which is a
different claim from "nothing worked".

Read-only. D13: reads `research.*`, writes nothing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.schema.schemas import (
    TABLE_RESEARCH_CANDIDATE_OUTCOME,
    TABLE_RESEARCH_CANDIDATE_POOL,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research/candidate-outcome", tags=["research-candidate-outcome"])


def build_summary(conn: Any, *, source: str | None = None, days: int = 90) -> dict[str, Any]:
    """Hit rate per horizon, plus how much of the pool is still unsettled."""
    where = ["c.trade_date >= CURRENT_DATE - %s::int"]
    params: list[Any] = [days]
    if source:
        where.append("c.source = %s")
        params.append(source)
    clause = " AND ".join(where)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT o.horizon_days,
                   count(*) AS settled,
                   count(*) FILTER (WHERE o.hit) AS hits,
                   count(*) FILTER (WHERE o.hit IS NOT NULL) AS judged,
                   avg(o.forward_return) AS avg_return,
                   avg(o.benchmark_return) AS avg_benchmark,
                   avg(o.excess_return) AS avg_excess
            FROM {TABLE_RESEARCH_CANDIDATE_OUTCOME} o
            JOIN {TABLE_RESEARCH_CANDIDATE_POOL} c ON c.id = o.candidate_id
            WHERE {clause}
            GROUP BY o.horizon_days
            ORDER BY o.horizon_days
            """,
            tuple(params),
        )
        rows = cur.fetchall() or []

        cur.execute(
            f"""
            SELECT count(*) FROM {TABLE_RESEARCH_CANDIDATE_POOL} c WHERE {clause}
            """,
            tuple(params),
        )
        pool_row = cur.fetchone()

    horizons = []
    for horizon, settled, hits, judged, avg_ret, avg_bench, avg_excess in rows:
        horizons.append(
            {
                "horizon_days": int(horizon),
                "settled": int(settled or 0),
                "judged": int(judged or 0),
                "hits": int(hits or 0),
                # None, not 0.0, when nothing has been judged — a 0% hit rate is
                # a finding, an unsettled ledger is not.
                "hit_rate": (float(hits) / float(judged)) if judged else None,
                "avg_return": float(avg_ret) if avg_ret is not None else None,
                "avg_benchmark": float(avg_bench) if avg_bench is not None else None,
                "avg_excess": float(avg_excess) if avg_excess is not None else None,
            }
        )

    candidates = int(pool_row[0]) if pool_row else 0
    settled_any = max((h["settled"] for h in horizons), default=0)
    return {
        "source": source,
        "days": days,
        "candidates": candidates,
        "horizons": horizons,
        "pending": max(0, candidates - settled_any),
    }


@router.get("/summary")
def get_summary(
    source: str | None = Query(None),
    days: int = Query(90, ge=1, le=730),
) -> dict[str, Any]:
    conn = connect()
    try:
        return {"ok": True, "data": build_summary(conn, source=source, days=days)}
    except Exception as exc:
        logger.warning("candidate_outcome summary failed: %s", exc)
        raise HTTPException(status_code=500, detail="candidate outcome summary failed") from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass


@router.get("/rows")
def get_rows(
    symbol: str | None = Query(None),
    horizon_days: int | None = Query(None, ge=1),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if symbol:
        where.append("o.symbol = %s")
        params.append(symbol.strip().upper())
    if horizon_days:
        where.append("o.horizon_days = %s")
        params.append(horizon_days)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT o.candidate_id, o.symbol, o.trade_date, o.horizon_days,
                       o.entry_close, o.exit_close, o.exit_date,
                       o.forward_return, o.benchmark_symbol, o.benchmark_return,
                       o.excess_return, o.hit, c.source
                FROM {TABLE_RESEARCH_CANDIDATE_OUTCOME} o
                JOIN {TABLE_RESEARCH_CANDIDATE_POOL} c ON c.id = o.candidate_id
                {clause}
                ORDER BY o.trade_date DESC, o.symbol, o.horizon_days
                LIMIT %s
                """,
                tuple(params),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall() or []]
        return {"ok": True, "data": {"rows": rows, "count": len(rows)}}
    except Exception as exc:
        logger.warning("candidate_outcome rows failed: %s", exc)
        raise HTTPException(status_code=500, detail="candidate outcome rows failed") from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass
