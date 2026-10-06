"""What happened to the candidates the Loop proposed.

GET /research/candidate-outcome/summary   — hit rate per horizon, sliceable by source
                                            (``by_regime=true`` adds the same per regime)
GET /research/candidate-outcome/rows      — settled legs, newest first, each with the
                                            regime its name stood in on the day

`hit` is "beat SPY over the same window", not "went up": candidates carry no
direction, and an absolute win rate mostly measures the market. Horizons that
have not elapsed are absent rather than zero, so `pending` is reported next to
`settled` — an empty ledger on a young pool means "not known yet", which is a
different claim from "nothing worked".

Regime (0.186.0, data gap R6.b) is read, not stored: the name's terrain regime
(``features.stock_forecast_terrain_daily.regime`` — ``range`` / ``trending`` /
``crash-risk``) on the candidate's ``trade_date``, or the latest session within
the week before it (a candidate dated on a weekend is settled from the prior
close, and is labelled from it too); SPY's on the same terms when the name has
none — the fallback ``/research/signal-decay`` uses. ``regime_scope`` says which
(``symbol`` / ``spy``) and ``regime_date`` which session; all three are null when
neither has a row. Measured 2026-10-06: 127 settled candidates, 111 labelled by
their own name, 16 by SPY, none unlabelled.

Read-only. D13: reads `research.*` and `features.*`, writes nothing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from bifrost_research.engines.backtest.catalog import evaluation
from bifrost_research.auth.deps import require_owner
from bifrost_research.db.conn import connect
from bifrost_research.schema.schemas import (
    TABLE_RESEARCH_CANDIDATE_OUTCOME,
    TABLE_RESEARCH_CANDIDATE_POOL,
    TABLE_STOCK_FORECAST_TERRAIN_DAILY,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research/candidate-outcome", tags=["research-candidate-outcome"])

# How far back a candidate's regime may be read from: a weekend or holiday date
# takes the last session before it, never one more than a week old.
REGIME_LOOKBACK_DAYS = 7

# Joins ``reg`` (regime, regime_scope, regime_date) onto a query over pool ``c``.
_REGIME_JOIN = f"""
    LEFT JOIN LATERAL (
        SELECT t.regime, t.scope AS regime_scope, t.trade_date AS regime_date
        FROM (
            SELECT ts.regime, 'symbol' AS scope, ts.trade_date, 0 AS pref
            FROM {TABLE_STOCK_FORECAST_TERRAIN_DAILY} ts
            WHERE ts.symbol = c.symbol AND ts.regime IS NOT NULL
              AND ts.trade_date <= c.trade_date
              AND ts.trade_date > c.trade_date - {REGIME_LOOKBACK_DAYS}
            UNION ALL
            SELECT tp.regime, 'spy' AS scope, tp.trade_date, 1 AS pref
            FROM {TABLE_STOCK_FORECAST_TERRAIN_DAILY} tp
            WHERE tp.symbol = 'SPY' AND tp.regime IS NOT NULL
              AND tp.trade_date <= c.trade_date
              AND tp.trade_date > c.trade_date - {REGIME_LOOKBACK_DAYS}
        ) t
        ORDER BY t.pref, t.trade_date DESC
        LIMIT 1
    ) reg ON true
"""


def _pool_filters(*, source: str | None, days: int | None) -> tuple[list[str], list[Any]]:
    """``source`` / ``days`` on pool ``c`` — the same meaning as ``build_summary``'s."""
    where: list[str] = []
    params: list[Any] = []
    if days is not None:
        where.append("c.trade_date >= CURRENT_DATE - %s::int")
        params.append(days)
    if source:
        where.append("c.source = %s")
        params.append(source)
    return where, params


def build_rows(
    conn: Any,
    *,
    symbol: str | None = None,
    horizon_days: int | None = None,
    source: str | None = None,
    days: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Settled legs, newest first; no ``source`` / ``days`` means no such filter."""
    where, params = _pool_filters(source=source, days=days)
    if symbol:
        where.append("o.symbol = %s")
        params.append(symbol.strip().upper())
    if horizon_days:
        where.append("o.horizon_days = %s")
        params.append(horizon_days)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT o.candidate_id, o.symbol, o.trade_date, o.horizon_days,
                   o.entry_close, o.exit_close, o.exit_date,
                   o.forward_return, o.benchmark_symbol, o.benchmark_return,
                   o.excess_return, o.hit, c.source,
                   reg.regime, reg.regime_scope, reg.regime_date
            FROM {TABLE_RESEARCH_CANDIDATE_OUTCOME} o
            JOIN {TABLE_RESEARCH_CANDIDATE_POOL} c ON c.id = o.candidate_id
            {_REGIME_JOIN}
            {clause}
            ORDER BY o.trade_date DESC, o.symbol, o.horizon_days
            LIMIT %s
            """,
            tuple(params),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall() or []]


def build_regime_breakdown(conn: Any, *, source: str | None = None, days: int = 90) -> list[dict[str, Any]]:
    """Hit rate per (regime, horizon) — the summary's per-horizon figures, split by regime.

    A candidate with no regime row is grouped under ``regime: null`` rather than
    dropped, so the groups add up to the summary's ``settled``.
    """
    where, params = _pool_filters(source=source, days=days)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT reg.regime, o.horizon_days,
                   count(*) AS settled,
                   count(*) FILTER (WHERE o.hit) AS hits,
                   count(*) FILTER (WHERE o.hit IS NOT NULL) AS judged,
                   avg(o.excess_return) AS avg_excess
            FROM {TABLE_RESEARCH_CANDIDATE_OUTCOME} o
            JOIN {TABLE_RESEARCH_CANDIDATE_POOL} c ON c.id = o.candidate_id
            {_REGIME_JOIN}
            WHERE {' AND '.join(where)}
            GROUP BY reg.regime, o.horizon_days
            ORDER BY reg.regime NULLS LAST, o.horizon_days
            """,
            tuple(params),
        )
        rows = cur.fetchall() or []
    return [
        {
            "regime": regime,
            "horizon_days": int(horizon),
            "settled": int(settled or 0),
            "judged": int(judged or 0),
            "hits": int(hits or 0),
            "hit_rate": (float(hits) / float(judged)) if judged else None,
            "avg_excess": float(avg_excess) if avg_excess is not None else None,
        }
        for regime, horizon, settled, hits, judged, avg_excess in rows
    ]


def build_summary(
    conn: Any,
    *,
    source: str | None = None,
    objective_id: str | None = None,
    days: int = 90,
) -> dict[str, Any]:
    """Hit rate per horizon, plus how much of the pool is still unsettled.

    ``objective_id`` narrows to the candidates one objective proposed (their
    ``source_ref.objective_id``) — the weekly policy review judges each
    objective on its own record, not on the pool every objective shares.
    """
    where = ["c.trade_date >= CURRENT_DATE - %s::int"]
    params: list[Any] = [days]
    if source:
        where.append("c.source = %s")
        params.append(source)
    if objective_id:
        where.append("c.source_ref->>'objective_id' = %s")
        params.append(objective_id)
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
        "objective_id": objective_id,
        "days": days,
        "candidates": candidates,
        "horizons": horizons,
        "pending": max(0, candidates - settled_any),
    }


@router.get("/summary", dependencies=[Depends(require_owner)])
def get_summary(
    source: str | None = Query(None),
    days: int = Query(90, ge=1, le=730),
    by_regime: bool = Query(False, description="Add data.by_regime: the same figures per terrain regime"),
) -> dict[str, Any]:
    conn = connect()
    try:
        data = build_summary(conn, source=source, days=days)
        if by_regime:
            data = {**data, "by_regime": build_regime_breakdown(conn, source=source, days=days)}
        return {
            "ok": True,
            "data": data,
            "evaluation": evaluation("candidate_outcome"),
        }
    except Exception as exc:
        logger.warning("candidate_outcome summary failed: %s", exc)
        raise HTTPException(status_code=500, detail="candidate outcome summary failed") from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass


@router.get("/rows", dependencies=[Depends(require_owner)])
def get_rows(
    symbol: str | None = Query(None),
    horizon_days: int | None = Query(None, ge=1),
    source: str | None = Query(None, description="Pool source (harness, copilot, scan, …); omit for all"),
    days: int | None = Query(
        None, ge=1, le=730, description="Candidates dated in the last N days, as /summary; omit for all"
    ),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    conn = connect()
    try:
        rows = build_rows(
            conn, symbol=symbol, horizon_days=horizon_days, source=source, days=days, limit=limit
        )
        return {
            "ok": True,
            "data": {"rows": rows, "count": len(rows)},
            "evaluation": evaluation("candidate_outcome"),
        }
    except Exception as exc:
        logger.warning("candidate_outcome rows failed: %s", exc)
        raise HTTPException(status_code=500, detail="candidate outcome rows failed") from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass
