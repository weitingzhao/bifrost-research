"""Saved-screen CRUD routes — 6A.

One screen, one id: the authoring face (frontend ``/research/lab/screener``)
writes here; Trade's result face renders the same object read-only by id.
Envelope matches the hypothesis routes: ``{"ok": bool, "data": ..., "error"?}``.

Routes:
    GET    /research/screens
    POST   /research/screens
    GET    /research/screens/{id}
    PATCH  /research/screens/{id}
    POST   /research/screens/{id}/retire
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.db.conn import connect
from bifrost_research.repositories import saved_screen as repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/screens", tags=["research-screens"])


class ScreenCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., min_length=1, max_length=120)
    definition: dict[str, Any]
    description: str | None = None
    origin_page: str | None = None


class ScreenPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    definition: dict[str, Any] | None = None
    is_active: bool | None = None
    origin_page: str | None = None


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:  # pragma: no cover - connection config issues
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


@router.get("")
def list_screens(include_retired: bool = Query(False)) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        rows = repo.list_screens(conn, include_retired=include_retired)
    finally:
        conn.close()
    return _ok({"screens": rows, "count": len(rows)})


@router.post("")
def create_screen(body: ScreenCreate) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        try:
            row = repo.create_screen(
                conn,
                name=body.name,
                definition=body.definition,
                description=body.description,
                origin_page=body.origin_page,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()
    return _ok(row)


@router.get("/{screen_id}")
def get_screen(screen_id: str) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        row = repo.get_screen(conn, screen_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"screen not found: {screen_id}")
    return _ok(row)


@router.patch("/{screen_id}")
def patch_screen(screen_id: str, body: ScreenPatch) -> dict[str, Any]:
    updates = body.model_dump(exclude_unset=True)
    conn = _connect_or_503()
    try:
        try:
            row = repo.patch_screen(conn, screen_id, updates)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"screen not found: {screen_id}")
    return _ok(row)


@router.post("/{screen_id}/retire")
def retire_screen(screen_id: str) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        row = repo.retire_screen(conn, screen_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"screen not found or already retired: {screen_id}")
    return _ok(row)
