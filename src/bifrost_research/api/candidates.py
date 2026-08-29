"""Candidate Pool CRUD — Wave Loop v1.

Routes:
    GET  /research/candidates
    POST /research/candidates
    POST /research/candidates/{id}/promote
    POST /research/candidates/{id}/dismiss
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.db.conn import connect
from bifrost_research.repositories import candidate_pool as repo
from bifrost_research.repositories import hypothesis as hyp_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/candidates", tags=["research-candidates"])


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


class CandidateItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str = Field(..., min_length=1, max_length=32)
    source: str = Field(default="manual")
    trade_date: date | None = None
    source_ref: dict[str, Any] | None = None
    score: float | None = None
    lens_snapshot: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)
    ttl_days: int = Field(default=5, ge=1, le=30)


class CandidateBatchCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[CandidateItem] = Field(..., min_length=1, max_length=50)
    owner_id: str = Field(default="owner")


class PromoteBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    thesis: str | None = None
    tags: list[str] = Field(default_factory=list)


@router.get("")
def list_candidates(
    status: str | None = Query(default="open"),
    source: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        try:
            repo.expire_stale(conn)
            rows = repo.list_candidates(
                conn,
                status=status,
                source=source,
                symbol=symbol,
                days=days,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
    return _ok({"items": rows, "count": len(rows)})


@router.post("")
def create_candidates(body: CandidateBatchCreate) -> dict[str, Any]:
    conn = _connect_or_503()
    created: list[dict[str, Any]] = []
    try:
        for item in body.items:
            try:
                row = repo.create_candidate(
                    conn,
                    symbol=item.symbol,
                    source=item.source,
                    trade_date=item.trade_date,
                    source_ref=item.source_ref,
                    score=item.score,
                    lens_snapshot=item.lens_snapshot,
                    tags=item.tags,
                    owner_id=body.owner_id,
                    ttl_days=item.ttl_days,
                )
                created.append(row)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
    return _ok({"items": created, "count": len(created)})


@router.post("/{candidate_id}/promote")
def promote_candidate(candidate_id: str, body: PromoteBody | None = None) -> dict[str, Any]:
    body = body or PromoteBody()
    conn = _connect_or_503()
    try:
        cand = repo.get_candidate(conn, candidate_id)
        if cand is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        if cand.get("status") != "open":
            raise HTTPException(status_code=400, detail=f"candidate status is {cand.get('status')}")

        sym = cand["symbol"]
        title = (body.title or f"{sym} candidate").strip()
        thesis = (
            body.thesis
            or f"Promoted from candidate pool ({cand.get('source')}) on {cand.get('trade_date')}."
        ).strip()
        tags = list(body.tags) or list(cand.get("tags") or [])
        if "from-candidate" not in tags:
            tags.append("from-candidate")

        hyp = hyp_repo.create_hypothesis(
            conn,
            title=title,
            thesis=thesis,
            symbols=[sym],
            tags=tags,
            status="active",
            origin_page="candidate-pool",
            origin_ref={
                "candidate_id": candidate_id,
                "source": cand.get("source"),
                "lens_snapshot": cand.get("lens_snapshot") or {},
                "score": cand.get("score"),
            },
        )
        updated = repo.promote_candidate(conn, candidate_id, hypothesis_id=hyp["id"])
        if updated is None:
            raise HTTPException(status_code=400, detail="promote failed")
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("promote candidate failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        conn.close()
    return _ok({"candidate": updated, "hypothesis": hyp})


@router.post("/{candidate_id}/dismiss")
def dismiss_candidate(candidate_id: str) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        updated = repo.dismiss_candidate(conn, candidate_id)
        if updated is None:
            existing = repo.get_candidate(conn, candidate_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="candidate not found")
            raise HTTPException(
                status_code=400,
                detail=f"cannot dismiss status={existing.get('status')}",
            )
    finally:
        conn.close()
    return _ok(updated)
