"""Journal notes API (K4 — design Rev .96/.97, contracts Spec §20).

⌥N writes here; the Journal › Notes view reads here. Notes are keyed by the
research user (§20.5) and stay editable and deletable until the nightly
distillation references them (§20.1) — a locked mutation answers 409 with the
memory id, which is exactly what the row's «→ memory M-xx» mark shows.

TD-73: a trade ref is stored per environment (``prod:158``). The environment
comes from the ``X-Bifrost-Env`` header each Trade gateway stamps on the way
here; a trade ref or trade filter without it is a 400, never a guess. Notes
that link no trade work with or without the header.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bifrost_research.api.dagster_launch import launch_job
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


def request_env(
    x_bifrost_env: str | None = Header(default=None, alias=repo.ENV_HEADER),
) -> str | None:
    """The environment the Trade gateway stamped (TD-73); None when absent."""
    try:
        return repo.parse_env(x_bifrost_env)
    except repo.TradeRefEnvError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _refs(raw: Any, env: str | None) -> list[dict[str, str]]:
    try:
        return repo.normalize_refs(raw, env)
    except repo.TradeRefEnvError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
def note_create(
    body: NoteBody,
    owner_id: str = Depends(require_owner),
    env: str | None = Depends(request_env),
) -> dict[str, Any]:
    text = _clean_body(body.body_md)
    refs = _refs(body.refs, env)
    conn = connect()
    try:
        note = repo.insert_note(
            conn,
            owner_id=owner_id,
            body_md=text,
            page_route=(body.page_route or "").strip()[:300],
            page_label=(body.page_label or "").strip()[:120],
            refs=refs,
            env=env,
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
    env: str | None = Depends(request_env),
) -> dict[str, Any]:
    if ref_type is not None and ref_type not in repo.REF_TYPES:
        raise HTTPException(status_code=400, detail=f"ref_type must be one of {repo.REF_TYPES}")
    want_id = (ref_id or "").strip() or None
    if ref_type is not None and want_id is not None and repo.is_trade_ref_type(ref_type):
        try:
            ref_type, want_id = repo.TRADE_REF, repo.trade_filter_id(want_id, env)
        except repo.TradeRefEnvError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn = connect()
    try:
        rows = repo.list_notes(
            conn,
            owner_id=owner_id,
            q=(q or "").strip() or None,
            ref_type=ref_type,
            ref_id=want_id,
            before=before,
            limit=limit,
            env=env,
        )
        return _ok({"notes": rows, "count": len(rows)})
    finally:
        conn.close()


@router.patch("/notes/{note_id}")
def note_update_route(
    note_id: str,
    body: NotePatch,
    owner_id: str = Depends(require_owner),
    env: str | None = Depends(request_env),
) -> dict[str, Any]:
    if body.body_md is None and body.refs is None:
        raise HTTPException(status_code=400, detail="nothing to change")
    text = _clean_body(body.body_md) if body.body_md is not None else None
    refs = _refs(body.refs, env) if body.refs is not None else None
    conn = connect()
    try:
        note = repo.update_note(
            conn,
            owner_id=owner_id,
            note_id=note_id,
            body_md=text,
            refs=refs,
            env=env,
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


# ── K6 — memory, visits, hints, the Day view (Spec §20) ─────────────────────


class VisitBody(BaseModel):
    route: str
    symbol: str = ""


class SourceBody(BaseModel):
    enabled: bool


@router.get("/memory")
def memory_index(
    include_archived: bool = False,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    """The You page's payload: memories, the portrait axes (archived rows
    still back them, §20.3), sources, dismissal counts, the week."""
    from datetime import datetime

    from bifrost_research.engines.journal_distill import SOURCES
    from bifrost_research.repositories import journal_memory as mem

    conn = connect()
    try:
        all_rows = mem.list_memories(conn, owner_id=owner_id, include_archived=True)
        visible = [m for m in all_rows if include_archived or not m["archived"]]
        today = datetime.now().astimezone().date()
        return _ok(
            {
                "memories": visible,
                "archived_count": sum(1 for m in all_rows if m["archived"]),
                "axes": mem.portrait_axes(all_rows),
                "sources": mem.list_sources(conn, owner_id=owner_id, all_sources=SOURCES),
                "hints": mem.hint_counts(conn, owner_id=owner_id),
                "week": mem.week_summary(conn, owner_id=owner_id, today=today),
            }
        )
    finally:
        conn.close()


@router.delete("/memory/{mem_id}")
def memory_forget(mem_id: str, owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    """§20.2 — Forget is a topic tombstone: gone at once, never rewritten."""
    from bifrost_research.repositories import journal_memory as mem

    conn = connect()
    try:
        topic = mem.forget_memory(conn, owner_id=owner_id, mem_id=mem_id)
    finally:
        conn.close()
    if topic is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return _ok({"forgotten": mem_id, "topic": topic})


@router.put("/memory/sources/{source}")
def memory_source_set(
    source: str,
    body: SourceBody,
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    from bifrost_research.engines.journal_distill import SOURCES
    from bifrost_research.repositories import journal_memory as mem

    if source not in SOURCES:
        raise HTTPException(status_code=400, detail=f"source must be one of {SOURCES}")
    conn = connect()
    try:
        mem.set_source(conn, owner_id=owner_id, source=source, enabled=body.enabled)
        return _ok({"sources": mem.list_sources(conn, owner_id=owner_id, all_sources=SOURCES)})
    finally:
        conn.close()


@router.post("/visits")
def visit_create(body: VisitBody, owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    """The shell's beacon — one row per page dwell; raw rows roll 90 days."""
    route = (body.route or "").strip()[:300]
    if not route.startswith("/"):
        raise HTTPException(status_code=400, detail="route must start with /")
    from bifrost_research.repositories import journal_memory as mem

    conn = connect()
    try:
        mem.insert_visit(conn, owner_id=owner_id, route=route, symbol=(body.symbol or "")[:12])
        return _ok({"recorded": True})
    finally:
        conn.close()


@router.get("/memory/hint")
def memory_hint(symbol: str, owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    """The Plans page's memory hint for one name; quiet past the threshold."""
    from bifrost_research.repositories import journal_memory as mem

    conn = connect()
    try:
        hint = mem.hint_for_symbol(conn, owner_id=owner_id, symbol=symbol)
        return _ok({"hint": hint})
    finally:
        conn.close()


@router.post("/memory/hint/{topic}/dismiss")
def memory_hint_dismiss(topic: str, owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    """§20.6 — Not relevant. The count lives in the store; the You page shows
    it, and at HINT_QUIET_AT the hint stops offering itself."""
    from bifrost_research.repositories import journal_memory as mem

    conn = connect()
    try:
        count = mem.dismiss_hint(conn, owner_id=owner_id, topic=topic[:120])
        return _ok({"topic": topic[:120], "count": count, "quiet": count >= mem.HINT_QUIET_AT})
    finally:
        conn.close()


@router.get("/day")
def day_index(date: str | None = None, owner_id: str = Depends(require_owner)) -> dict[str, Any]:
    """The Journal's Day view: the raw trail of one trading day plus that
    day's memory changes. The end-of-day prose summary is owed by name."""
    from datetime import date as date_t, datetime

    from bifrost_research.repositories import journal_memory as mem

    if date:
        try:
            day = date_t.fromisoformat(date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    else:
        day = datetime.now().astimezone().date()
    conn = connect()
    try:
        return _ok(mem.day_view(conn, owner_id=owner_id, day=day))
    finally:
        conn.close()


@router.post("/memory/distill")
def memory_distill_run(owner_id: str = Depends(require_owner)) -> JSONResponse:
    """Start research_memory_distill_job. The nightly schedule is the normal writer."""
    del owner_id
    try:
        launched = launch_job("research_memory_distill_job")
    except Exception as exc:
        logger.exception("memory distill launch failed")
        raise HTTPException(status_code=502, detail=f"distill launch failed: {exc}") from exc
    return JSONResponse(status_code=202, content=_ok(launched))
