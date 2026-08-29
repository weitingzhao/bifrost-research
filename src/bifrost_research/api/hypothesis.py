"""Hypothesis CRUD routes — Wave RS-A · locks D-RS-a (Golden Source `research`).

Response envelope (per plan RESEARCH_MUSCLE_PLAN.md):
    { "ok": bool, "data": ..., "error"?: str }

Routes:
    GET    /research/hypothesis
    POST   /research/hypothesis
    GET    /research/hypothesis/{id}
    PATCH  /research/hypothesis/{id}
    POST   /research/hypothesis/{id}/retire
    POST   /research/hypothesis/{id}/refresh-trajectory  (Wave 13)
    GET    /research/hypothesis/summary/active

``origin_ref`` documented keys (soft validation on create/patch — extra keys allowed):
    watchlist_contract_key  Trade watchlist key, e.g. ``STK:NVDA`` (stock) or OPT:…
    trajectory_summary      Written by refresh-trajectory (structure, row counts, final_pnl)
    plus Analyze lens snapshot fields (symbol, date, vrp_pct, …)
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.db.conn import connect
from bifrost_research.engines.backtest.canonical_pnl import STRUCTURES
from bifrost_research.repositories import hypothesis as repo
from bifrost_research.schema.schemas import TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/hypothesis", tags=["research-hypothesis"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class HypothesisCreate(BaseModel):
    """Create body. ``origin_ref`` may include ``watchlist_contract_key`` and extras."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(..., min_length=1, max_length=200)
    thesis: str = Field(..., min_length=1)
    symbols: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    status: str | None = Field(default="active")
    origin_page: str | None = None
    origin_ref: Any = None
    linked_opportunity_ids: list[str] = Field(default_factory=list)
    linked_backtest_ids: list[str] = Field(default_factory=list)


class HypothesisPatch(BaseModel):
    """Patch body. Soft-validates ``origin_ref.watchlist_contract_key`` when present."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    thesis: str | None = None
    symbols: list[str] | None = None
    tags: list[str] | None = None
    status: str | None = None
    origin_page: str | None = None
    origin_ref: Any = None
    linked_opportunity_ids: list[str] | None = None
    linked_backtest_ids: list[str] | None = None
    conclusion: str | None = None

    def to_updates(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        raw = self.model_dump(exclude_unset=True)
        for key, value in raw.items():
            fields[key] = value
        return fields


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


def _parse_entry_date(created_at: Any) -> date:
    if isinstance(created_at, date) and not isinstance(created_at, datetime):
        return created_at
    if isinstance(created_at, datetime):
        return created_at.date()
    if isinstance(created_at, str) and created_at:
        # ISO datetime or date
        try:
            return date.fromisoformat(created_at[:10])
        except ValueError:
            pass
    return date.today()


def _fetch_trajectory_rows(
    conn: Any,
    *,
    symbol: str,
    entry_date: date,
    structure: str,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT as_of_date, entry_date, symbol, structure, params_hash,
                   structure_params, entry_spot, entry_atm_iv, entry_mid,
                   as_of_spot, as_of_atm_iv, mtm_value, pnl_since_entry,
                   dte_remaining, expired, final_pnl, data_quality
            FROM {TABLE_STOCK_SIGNAL_CANONICAL_PNL_DAILY}
            WHERE symbol = %s AND entry_date = %s AND structure = %s
            ORDER BY as_of_date ASC
            """,
            (symbol, entry_date, structure),
        )
        cols = [d[0] for d in cur.description]
        rows: list[dict[str, Any]] = []
        for r in cur.fetchall():
            item = dict(zip(cols, r))
            for k in ("as_of_date", "entry_date"):
                if item.get(k) is not None:
                    item[k] = item[k].isoformat()
            rows.append(item)
    return rows


def _trajectory_summary(rows: list[dict[str, Any]], *, structure: str, symbol: str, entry_date: date) -> dict[str, Any]:
    final_pnl = None
    last_pnl = None
    if rows:
        last = rows[-1]
        final_pnl = last.get("final_pnl")
        last_pnl = last.get("pnl_since_entry")
    return {
        "structure": structure,
        "symbol": symbol,
        "entry_date": entry_date.isoformat(),
        "row_count": len(rows),
        "final_pnl": final_pnl,
        "last_pnl_since_entry": last_pnl,
        "refreshed_at": datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def list_hypotheses(
    status: str | None = Query(None, description="active | validated | rejected | archived"),
    symbol: str | None = Query(None),
    tag: str | None = Query(None),
    include_retired: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        rows = repo.list_hypotheses(
            conn,
            status=status,
            symbol=symbol,
            tag=tag,
            include_retired=include_retired,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("list_hypotheses failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        conn.close()
    return _ok({"rows": rows, "count": len(rows), "limit": limit, "offset": offset})


@router.post("")
def create_hypothesis(body: HypothesisCreate) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        created = repo.create_hypothesis(
            conn,
            title=body.title,
            thesis=body.thesis,
            symbols=body.symbols,
            tags=body.tags,
            status=body.status,
            origin_page=body.origin_page,
            origin_ref=body.origin_ref,
            linked_opportunity_ids=body.linked_opportunity_ids,
            linked_backtest_ids=body.linked_backtest_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("create_hypothesis failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        conn.close()
    return _ok(created)


@router.get("/summary/active")
def summary_active(top_n: int = Query(5, ge=1, le=50)) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        summary = repo.active_summary(conn, top_n=top_n)
    except Exception as exc:
        logger.exception("active_summary failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        conn.close()
    return _ok(summary)


@router.get("/{hypothesis_id}")
def get_hypothesis(hypothesis_id: str) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        row = repo.get_hypothesis(conn, hypothesis_id)
    except Exception as exc:
        logger.exception("get_hypothesis failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"hypothesis {hypothesis_id} not found")
    return _ok(row)


@router.patch("/{hypothesis_id}")
def patch_hypothesis(hypothesis_id: str, body: HypothesisPatch) -> dict[str, Any]:
    updates = body.to_updates()
    conn = _connect_or_503()
    try:
        row = repo.patch_hypothesis(conn, hypothesis_id, updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("patch_hypothesis failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"hypothesis {hypothesis_id} not found")
    return _ok(row)


@router.post("/{hypothesis_id}/retire")
def retire_hypothesis(hypothesis_id: str) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        row = repo.retire_hypothesis(conn, hypothesis_id)
    except Exception as exc:
        logger.exception("retire_hypothesis failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        conn.close()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"hypothesis {hypothesis_id} not found or already retired",
        )
    return _ok(row)


@router.post("/{hypothesis_id}/refresh-trajectory")
def refresh_trajectory(
    hypothesis_id: str,
    structure: str = Query("short_strangle"),
) -> dict[str, Any]:
    """Load canonical PnL trajectory for hypothesis symbols + created_at date.

    Default structure is ``short_strangle``. Stores a summary into
    ``origin_ref.trajectory_summary`` (merged) and returns trajectory rows.
    """
    if structure not in STRUCTURES:
        raise HTTPException(status_code=400, detail=f"unknown structure: {structure}")
    conn = _connect_or_503()
    try:
        hyp = repo.get_hypothesis(conn, hypothesis_id)
        if hyp is None:
            raise HTTPException(status_code=404, detail=f"hypothesis {hypothesis_id} not found")
        symbols = hyp.get("symbols") or []
        if not symbols:
            raise HTTPException(status_code=400, detail="hypothesis has no symbols")
        symbol = str(symbols[0]).strip().upper()
        entry_date = _parse_entry_date(hyp.get("created_at"))
        rows = _fetch_trajectory_rows(
            conn, symbol=symbol, entry_date=entry_date, structure=structure
        )
        summary = _trajectory_summary(
            rows, structure=structure, symbol=symbol, entry_date=entry_date
        )
        merged = repo.merge_origin_ref(hyp.get("origin_ref"), {"trajectory_summary": summary})
        updated = repo.patch_hypothesis(conn, hypothesis_id, {"origin_ref": merged})
        return _ok(
            {
                "hypothesis": updated,
                "symbol": symbol,
                "entry_date": entry_date.isoformat(),
                "structure": structure,
                "rows": rows,
                "count": len(rows),
                "trajectory_summary": summary,
            }
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("refresh_trajectory failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


__all__ = ["router", "HypothesisCreate", "HypothesisPatch"]
