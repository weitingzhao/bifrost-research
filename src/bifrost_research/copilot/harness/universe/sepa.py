"""SEPA universe adapter — reads features.stock_signal_sepa_daily."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Protocol

from bifrost_research.db.conn import rollback_quietly
from bifrost_research.copilot.harness.policy_schema import LoopPolicy, SepaLayerPolicy

logger = logging.getLogger(__name__)

_SEPA_COLS = (
    "symbol",
    "trade_date",
    "sepa_score",
    "grade",
    "stage",
    "path",
    "momentum_score",
    "structure_score",
    "fundamental_score",
    "trend_template_score",
    "latest_close",
)


class _Connection(Protocol):
    def cursor(self) -> Any: ...


def _latest_trade_date(conn: _Connection) -> date | None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(trade_date) FROM features.stock_signal_sepa_daily")
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            val = row[0]
            return val if isinstance(val, date) else date.fromisoformat(str(val)[:10])
    except Exception as exc:  # noqa: BLE001
        rollback_quietly(conn)
        logger.warning("sepa latest_trade_date failed: %s", exc)
        return None


def fetch_sepa_symbols(
    conn: _Connection,
    *,
    layer: SepaLayerPolicy | None = None,
    limit: int = 500,
) -> tuple[list[str], dict[str, dict[str, Any]], str]:
    """Return symbols + row meta ordered by sepa_score DESC."""
    layer = layer or SepaLayerPolicy()
    trade_date = _latest_trade_date(conn)
    if trade_date is None:
        return [], {}, "no sepa data"

    clauses = ["trade_date = %s"]
    params: list[Any] = [trade_date]

    paths = layer.stage  # stage field in API is stage enum; path is SETUP/PIVOT
    # layer.stage holds path values SETUP/PIVOT per LS-1 schema (Explorer path filter)
    path_values = [p.strip().upper() for p in layer.stage if p.strip()]
    if path_values:
        placeholders = ", ".join(["%s"] * len(path_values))
        clauses.append(f"path IN ({placeholders})")
        params.extend(path_values)
    if layer.path:
        clauses.append("path = %s")
        params.append(layer.path.strip().upper())
    if layer.grade:
        clauses.append("grade = %s")
        params.append(layer.grade.strip().upper())
    if layer.min_score is not None:
        clauses.append("sepa_score >= %s")
        params.append(float(layer.min_score))

    sql = f"""
        SELECT {", ".join(_SEPA_COLS)}
        FROM features.stock_signal_sepa_daily
        WHERE {" AND ".join(clauses)}
        ORDER BY sepa_score DESC NULLS LAST, symbol ASC
        LIMIT %s
    """
    params.append(max(1, min(limit, 2000)))

    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall() or []]
    except Exception as exc:  # noqa: BLE001
        rollback_quietly(conn)
        logger.warning("fetch_sepa_symbols failed: %s", exc)
        return [], {}, f"query failed: {exc}"

    meta: dict[str, dict[str, Any]] = {}
    symbols: list[str] = []
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        if sym not in meta:
            symbols.append(sym)
            meta[sym] = {**row, "sepa_score": row.get("sepa_score")}

    filt = f"path IN {path_values or 'any'}, min_score>={layer.min_score}"
    if layer.grade:
        filt += f", grade={layer.grade}"
    return symbols, meta, filt


def resolve_sepa_only(
    conn: _Connection,
    policy: LoopPolicy,
    *,
    limit: int,
) -> tuple[list[str], dict[str, dict[str, Any]], str]:
    return fetch_sepa_symbols(conn, layer=policy.layers.sepa, limit=limit)
