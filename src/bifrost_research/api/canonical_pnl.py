"""Canonical structure PnL HTTP routes — Wave Canonical-PnL Foundation.

Routes:
  GET /research/canonical-pnl/trajectory?symbol=&entry_date=&structure=
  GET /research/canonical-pnl/coverage
  GET /research/canonical-pnl/structures
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.engines.backtest.canonical_pnl import STRUCTURES
from bifrost_research.engines.canonical_pnl import coverage_report
from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research/canonical-pnl", tags=["research-canonical-pnl"])


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


@router.get("/structures")
def list_structures() -> dict[str, Any]:
    return _ok({"structures": list(STRUCTURES)})


@router.get("/coverage")
def coverage() -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        return _ok(coverage_report(conn))
    except Exception as exc:
        logger.exception("canonical-pnl/coverage failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("/trajectory")
def trajectory(
    symbol: str = Query(..., min_length=1, max_length=32),
    entry_date: date = Query(...),
    structure: str = Query("short_strangle"),
    params_hash: str | None = Query(None),
) -> dict[str, Any]:
    if structure not in STRUCTURES:
        raise HTTPException(status_code=400, detail=f"unknown structure: {structure}")
    conn = _connect_or_503()
    try:
        with conn.cursor() as cur:
            sql = f"""
                SELECT as_of_date, entry_date, symbol, structure, params_hash,
                       structure_params, entry_spot, entry_atm_iv, entry_mid,
                       as_of_spot, as_of_atm_iv, mtm_value, pnl_since_entry,
                       dte_remaining, expired, final_pnl, data_quality
                FROM {TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY}
                WHERE symbol = %s AND entry_date = %s AND structure = %s
            """
            args: list[Any] = [symbol.strip().upper(), entry_date, structure]
            if params_hash:
                sql += " AND params_hash = %s"
                args.append(params_hash)
            sql += " ORDER BY as_of_date ASC"
            cur.execute(sql, args)
            cols = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall():
                item = dict(zip(cols, r))
                for k in ("as_of_date", "entry_date"):
                    if item.get(k) is not None:
                        item[k] = item[k].isoformat()
                rows.append(item)
        return _ok(
            {
                "symbol": symbol.strip().upper(),
                "entry_date": entry_date.isoformat(),
                "structure": structure,
                "rows": rows,
                "count": len(rows),
            }
        )
    except Exception as exc:
        logger.exception("canonical-pnl/trajectory failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass
