"""journal.note — the trader's own notes (K4, D-Journal-Stores, Spec §20).

One row per ⌥N. Keyed by research user (§20.5: the portrait is the person's,
so its raw material is too). A note is free to edit and delete until the
nightly distillation references it as evidence — `distilled_memory_id` is that
lock (§20.1); K6's distiller is the only writer of the column, and the two
mutating functions here refuse locked rows so the rule cannot be bypassed by
a caller that forgot to check.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg2.extras import RealDictCursor

from bifrost_research.schema.schemas import TABLE_JOURNAL_NOTE

REF_TYPES = ("sym", "obj", "inst")
MAX_REFS = 12
MAX_BODY_CHARS = 20_000


class NoteLockedError(Exception):
    """The nightly distillation holds this note as evidence (§20.1)."""


def normalize_refs(raw: Any) -> list[dict[str, str]]:
    """The design's shape — ``[{type: sym|obj|inst, id}]`` — and nothing else.

    Unknown types, blank ids and duplicates fall out rather than erroring:
    the shell composes refs automatically (page symbol, current objective),
    and a half-good list is a better note than a rejected one.
    """
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or "").strip()
        i = str(item.get("id") or "").strip()
        if t not in REF_TYPES or not i:
            continue
        if t == "sym":
            i = i.upper()
        key = (t, i)
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": t, "id": i})
        if len(out) >= MAX_REFS:
            break
    return out


def _row(r: dict[str, Any]) -> dict[str, Any]:
    refs = r.get("refs")
    return {
        "id": str(r["id"]),
        "owner_id": r["owner_id"],
        "body_md": r["body_md"],
        "page_route": r.get("page_route") or "",
        "page_label": r.get("page_label") or "",
        "refs": refs if isinstance(refs, list) else [],
        "distilled_memory_id": r.get("distilled_memory_id"),
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
    }


def insert_note(
    conn: Any,
    *,
    owner_id: str,
    body_md: str,
    page_route: str = "",
    page_label: str = "",
    refs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_JOURNAL_NOTE}
                (owner_id, body_md, page_route, page_label, refs)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            RETURNING *
            """,
            (owner_id, body_md, page_route, page_label, json.dumps(refs or [])),
        )
        row = cur.fetchone()
    conn.commit()
    return _row(dict(row))


def list_notes(
    conn: Any,
    *,
    owner_id: str,
    q: str | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
    before: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Newest first — the Notes view groups by day on the way out."""
    clauses = ["owner_id = %s"]
    params: list[Any] = [owner_id]
    if q:
        clauses.append("body_md ILIKE %s")
        params.append(f"%{q}%")
    if ref_type and ref_id:
        # jsonb_path_ops GIN answers @> directly.
        clauses.append("refs @> %s::jsonb")
        params.append(json.dumps([{"type": ref_type, "id": ref_id}]))
    if before:
        clauses.append("created_at < %s")
        params.append(before)
    params.append(max(1, min(int(limit), 500)))
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT * FROM {TABLE_JOURNAL_NOTE}
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall() or []
    return [_row(dict(r)) for r in rows]


def _fetch_owned(cur: Any, note_id: str, owner_id: str) -> dict[str, Any] | None:
    cur.execute(
        f"SELECT * FROM {TABLE_JOURNAL_NOTE} WHERE id = %s AND owner_id = %s",
        (note_id, owner_id),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def update_note(
    conn: Any,
    *,
    owner_id: str,
    note_id: str,
    body_md: str | None = None,
    refs: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """None = not found. NoteLockedError = distilled (§20.1)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        row = _fetch_owned(cur, note_id, owner_id)
        if row is None:
            return None
        if row.get("distilled_memory_id"):
            raise NoteLockedError(str(row["distilled_memory_id"]))
        sets = ["updated_at = now()"]
        params: list[Any] = []
        if body_md is not None:
            sets.append("body_md = %s")
            params.append(body_md)
        if refs is not None:
            sets.append("refs = %s::jsonb")
            params.append(json.dumps(refs))
        params.extend([note_id, owner_id])
        cur.execute(
            f"""
            UPDATE {TABLE_JOURNAL_NOTE}
            SET {", ".join(sets)}
            WHERE id = %s AND owner_id = %s
            RETURNING *
            """,
            params,
        )
        updated = cur.fetchone()
    conn.commit()
    return _row(dict(updated)) if updated else None


def delete_note(conn: Any, *, owner_id: str, note_id: str) -> bool:
    """False = not found. NoteLockedError = distilled (§20.1)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        row = _fetch_owned(cur, note_id, owner_id)
        if row is None:
            return False
        if row.get("distilled_memory_id"):
            raise NoteLockedError(str(row["distilled_memory_id"]))
        cur.execute(
            f"DELETE FROM {TABLE_JOURNAL_NOTE} WHERE id = %s AND owner_id = %s",
            (note_id, owner_id),
        )
    conn.commit()
    return True
