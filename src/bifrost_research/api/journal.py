"""Journal notes API (K4 — design Rev .96/.97, contracts Spec §20).

⌥N writes here; the Journal › Notes view reads here. Notes are keyed by the
research user (§20.5) and stay editable and deletable until the nightly
distillation references them (§20.1) — a locked mutation answers 409 with the
memory id, which is exactly what the row's «→ memory M-xx» mark shows.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from bifrost_research.auth.deps import require_owner
from bifrost_research.db.conn import connect
from bifrost_research.repositories import journal_notes as repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/journal", tags=["research-journal"])


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


class NoteBody(BaseModel):
    body_md: str
    page_route: str = ""
    page_label: str = ""
    refs: list[dict[str, Any]] = []


class NotePatch(BaseModel):
    body_md: str | None = None
    refs: list[dict[str, Any]] | None = None


def _clean_body(raw: str) -> str:
    body = (raw or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="body_md is required")
    if len(body) > repo.MAX_BODY_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"body_md over {repo.MAX_BODY_CHARS} chars",
        )
    return body


@router.post("/notes")
def note_create(body: NoteBody, owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    text = _clean_body(body.body_md)
    conn = connect()
    try:
        note = repo.insert_note(
            conn,
            owner_id=owner_id,
            body_md=text,
            page_route=(body.page_route or "").strip()[:300],
            page_label=(body.page_label or "").strip()[:120],
            refs=repo.normalize_refs(body.refs),
        )
        return _ok({"note": note})
    finally:
        conn.close()


@router.get("/notes")
def note_index(
    q: str | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
    before: str | None = None,
    limit: int = 200,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    if ref_type is not None and ref_type not in repo.REF_TYPES:
        raise HTTPException(status_code=400, detail=f"ref_type must be one of {repo.REF_TYPES}")
    conn = connect()
    try:
        rows = repo.list_notes(
            conn,
            owner_id=owner_id,
            q=(q or "").strip() or None,
            ref_type=ref_type,
            ref_id=(ref_id or "").strip() or None,
            before=before,
            limit=limit,
        )
        return _ok({"notes": rows, "count": len(rows)})
    finally:
        conn.close()


@router.patch("/notes/{note_id}")
def note_update_route(
    note_id: str,
    body: NotePatch,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    if body.body_md is None and body.refs is None:
        raise HTTPException(status_code=400, detail="nothing to change")
    conn = connect()
    try:
        note = repo.update_note(
            conn,
            owner_id=owner_id,
            note_id=note_id,
            body_md=_clean_body(body.body_md) if body.body_md is not None else None,
            refs=repo.normalize_refs(body.refs) if body.refs is not None else None,
        )
    except repo.NoteLockedError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"note is distilled into memory {exc} — locked (§20.1)",
        ) from exc
    finally:
        conn.close()
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")
    return _ok({"note": note})


@router.delete("/notes/{note_id}")
def note_remove(note_id: str, owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    conn = connect()
    try:
        gone = repo.delete_note(conn, owner_id=owner_id, note_id=note_id)
    except repo.NoteLockedError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"note is distilled into memory {exc} — locked (§20.1)",
        ) from exc
    finally:
        conn.close()
    if not gone:
        raise HTTPException(status_code=404, detail="note not found")
    return _ok({"deleted": note_id})
