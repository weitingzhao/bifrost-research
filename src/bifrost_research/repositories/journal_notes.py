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
import re
from typing import Any

from psycopg2.extras import RealDictCursor

from bifrost_research.schema.schemas import TABLE_JOURNAL_NOTE

# A trade's ref is stored as ``trade`` (naming R0, design Rev .111); ``inst`` is
# its old code, still accepted on write and read for one release and stored as
# ``trade`` so the two never split one trade's notes.
TRADE_REF = "trade"
LEGACY_TRADE_REFS = ("inst",)
REF_TYPES = ("sym", "obj", TRADE_REF, *LEGACY_TRADE_REFS)
MAX_REFS = 12
MAX_BODY_CHARS = 20_000

# TD-73: one Research serves DEV, STG and PROD, and a trade id is only unique
# inside one environment's Trade database (DEV #158 and PROD #158 are different
# trades). The Trade gateway of each environment stamps this header on the way
# to Research (a Traefik middleware that overwrites whatever the browser sent),
# and a trade ref is stored as ``<env>:<id>`` — ``prod:158``.
ENV_HEADER = "X-Bifrost-Env"
ENVS = ("dev", "stg", "prod")

_TRADE_ID = re.compile(r"^#?(\d{1,12})$")
_QUALIFIED_TRADE_ID = re.compile(r"^(dev|stg|prod):(\d{1,12})$")


class NoteLockedError(Exception):
    """The nightly distillation holds this note as evidence (§20.1)."""


class TradeRefEnvError(ValueError):
    """A trade ref that cannot be tied to one environment's trade."""


def is_trade_ref_type(t: str) -> bool:
    return t == TRADE_REF or t in LEGACY_TRADE_REFS


def parse_env(raw: str | None) -> str | None:
    """The request's environment from :data:`ENV_HEADER`; None when absent.

    A value outside :data:`ENVS` is a gateway misconfiguration, not a guess to
    make — :class:`TradeRefEnvError`.
    """
    v = (raw or "").strip().lower()
    if not v:
        return None
    if v not in ENVS:
        raise TradeRefEnvError(f"{ENV_HEADER} must be one of {ENVS}, got {raw!r}")
    return v


def qualify_trade_id(raw_id: str, env: str | None) -> str | None:
    """A trade id as stored: ``<env>:<id>``. None = not a trade id (the ref falls out).

    A bare id (``158`` / ``#158``) takes the request's environment; without one
    there is no telling which trade it is, so :class:`TradeRefEnvError`. An id
    already qualified is accepted only for the request's own environment — a
    page in PROD cannot link a DEV trade.
    """
    s = raw_id.strip()
    bare = _TRADE_ID.match(s)
    if bare:
        if env is None:
            raise TradeRefEnvError(
                f"a trade ref needs the {ENV_HEADER} header (dev|stg|prod): "
                "trade ids repeat across environments"
            )
        return f"{env}:{int(bare.group(1))}"
    qualified = _QUALIFIED_TRADE_ID.match(s)
    if qualified:
        if qualified.group(1) != env:
            raise TradeRefEnvError(
                f"trade ref {s!r} is not this environment's ({env or 'no ' + ENV_HEADER})"
            )
        return f"{qualified.group(1)}:{int(qualified.group(2))}"
    return None


def trade_filter_id(raw_id: str, env: str | None) -> str:
    """The stored id a ``ref_type=trade`` filter looks for.

    A bare id takes the request's environment (none → :class:`TradeRefEnvError`);
    a qualified one is read as given — reading another environment's notes
    harms nothing, and the Notes view offers them as chips.
    """
    s = raw_id.strip()
    qualified = _QUALIFIED_TRADE_ID.match(s)
    if qualified:
        return f"{qualified.group(1)}:{int(qualified.group(2))}"
    bare = _TRADE_ID.match(s)
    if not bare:
        raise TradeRefEnvError(f"ref_id {raw_id!r} is not a trade id")
    if env is None:
        raise TradeRefEnvError(
            f"a trade filter needs the {ENV_HEADER} header (dev|stg|prod): "
            "trade ids repeat across environments"
        )
    return f"{env}:{int(bare.group(1))}"


def normalize_refs(raw: Any, env: str | None = None) -> list[dict[str, str]]:
    """The design's shape — ``[{type: sym|obj|trade, id}]`` — and nothing else.

    Unknown types, blank ids and duplicates fall out rather than erroring:
    the shell composes refs automatically (page symbol, current objective),
    and a half-good list is a better note than a rejected one. A trade ref is
    the exception: one that cannot be tied to ``env`` raises
    :class:`TradeRefEnvError` (TD-73) — linking the wrong environment's trade
    is worse than no note.
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
        elif is_trade_ref_type(t):
            t = TRADE_REF
            q = qualify_trade_id(i, env)
            if q is None:
                continue
            i = q
        key = (t, i)
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": t, "id": i})
        if len(out) >= MAX_REFS:
            break
    return out


def present_refs(refs: Any, env: str | None) -> list[dict[str, Any]]:
    """Refs as a page in ``env`` reads them: its own trades bare (``158``), any
    other environment's still qualified (``dev:158``) so they never pass for
    one of this environment's trades. Without ``env`` every ref is as stored."""
    out: list[dict[str, Any]] = []
    own = f"{env}:" if env else None
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict):
            continue
        rid = str(ref.get("id") or "")
        if own and is_trade_ref_type(str(ref.get("type") or "")) and rid.startswith(own):
            ref = {**ref, "id": rid[len(own) :]}
        out.append(ref)
    return out


def _row(r: dict[str, Any], env: str | None = None) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "owner_id": r["owner_id"],
        "body_md": r["body_md"],
        "page_route": r.get("page_route") or "",
        "page_label": r.get("page_label") or "",
        "refs": present_refs(r.get("refs"), env),
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
    env: str | None = None,
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
    return _row(dict(row), env)


def list_notes(
    conn: Any,
    *,
    owner_id: str,
    q: str | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
    before: str | None = None,
    limit: int = 200,
    env: str | None = None,
) -> list[dict[str, Any]]:
    """Newest first — the Notes view groups by day on the way out.

    ``ref_type`` / ``ref_id`` are as stored (the route resolves a trade filter
    with :func:`trade_filter_id`); ``env`` only shapes the refs on the way out."""
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
    return [_row(dict(r), env) for r in rows]


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
    env: str | None = None,
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
    return _row(dict(updated), env) if updated else None


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
