"""Momentum universe adapter — reads features.stock_signal_momentum_daily."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Protocol

from bifrost_research.copilot.harness.policy_schema import LoopPolicy, MomentumLayerPolicy

logger = logging.getLogger(__name__)

_MOMENTUM_COLS = (
    "symbol",
    "trade_date",
    "score",
    "grade",
    "path",
)


class _Connection(Protocol):
    def cursor(self) -> Any: ...


def _latest_trade_date(conn: _Connection) -> date | None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(trade_date) FROM features.stock_signal_momentum_daily")
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            val = row[0]
            return val if isinstance(val, date) else date.fromisoformat(str(val)[:10])
    except Exception as exc:  # noqa: BLE001
        logger.warning("momentum latest_trade_date failed: %s", exc)
        return None


def fetch_momentum_symbols(
    conn: _Connection,
    *,
    layer: MomentumLayerPolicy | None = None,
    limit: int = 500,
) -> tuple[list[str], dict[str, dict[str, Any]], str]:
    layer = layer or MomentumLayerPolicy()
    trade_date = _latest_trade_date(conn)
    if trade_date is None:
        return [], {}, "no momentum data"

    clauses = ["trade_date = %s"]
    params: list[Any] = [trade_date]
    if layer.grade:
        clauses.append("grade = %s")
        params.append(layer.grade.strip().upper())
    if layer.path:
        clauses.append("path = %s")
        params.append(layer.path.strip().upper())
    if layer.min_score is not None:
        clauses.append("score >= %s")
        params.append(float(layer.min_score))

    sql = f"""
        SELECT {", ".join(_MOMENTUM_COLS)}
        FROM features.stock_signal_momentum_daily
        WHERE {" AND ".join(clauses)}
        ORDER BY score DESC NULLS LAST, symbol ASC
        LIMIT %s
    """
    params.append(max(1, min(limit, 2000)))

    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall() or []]
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_momentum_symbols failed: %s", exc)
        return [], {}, f"query failed: {exc}"

    meta: dict[str, dict[str, Any]] = {}
    symbols: list[str] = []
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        if sym not in meta:
            symbols.append(sym)
            meta[sym] = {**row, "momentum_score": row.get("score")}

    filt = "momentum"
    if layer.grade:
        filt += f" grade={layer.grade}"
    if layer.min_score is not None:
        filt += f" min_score>={layer.min_score}"
    return symbols, meta, filt


def resolve_momentum_only(
    conn: _Connection,
    policy: LoopPolicy,
    *,
    limit: int,
) -> tuple[list[str], dict[str, dict[str, Any]], str]:
    return fetch_momentum_symbols(conn, layer=policy.layers.momentum, limit=limit)
