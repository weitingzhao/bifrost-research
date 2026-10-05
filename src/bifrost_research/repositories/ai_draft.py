"""SQL layer for ``research.ai_draft`` — Wave RS-E3 Cockpit inbox (D-RS-E-e)."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from bifrost_research.schema.schemas import TABLE_RESEARCH_AI_DRAFT

_ALLOWED_STATUSES = frozenset({"pending", "approved", "dismissed", "expired"})
_ALLOWED_KINDS = frozenset(
    {
        "morning_brief",
        "eod_verdict",
        # research-loop-automation D2 — one digest per trading day
        "daily_digest",
        "hypothesis_suggestion",
        "playbook_rule",
        "playbook_note",
        # Wave C / A / O — Research Loop
        "candidate_batch",
        "hypothesis_draft",
        "decision_draft",
        "order_intent",
        # Wave Y.3 shipped the policy_suggestion draft without adding it here.
        # scan_legacy never reached that branch (use_llm_plan was off), so the
        # first stock_composite run — which turns the LLM plan on — failed with
        # "invalid ai_draft kind". tests/repositories/test_draft_kind_contract.py
        # now derives this set from the call sites.
        "policy_suggestion",
    }
)

_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "payload",
    "scope",
    "status",
    "generated_by",
    "linked_action_id",
    "created_at",
    "expires_at",
)

_JSON_COLS = frozenset({"payload"})
_TS_COLS = frozenset({"created_at", "expires_at"})


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def generate_draft_id() -> str:
    ts = int(time.time() * 1000)
    return f"drf_{ts:x}{secrets.token_hex(4)}"


def _validate_status(status: str) -> str:
    if status not in _ALLOWED_STATUSES:
        raise ValueError(f"invalid ai_draft status: {status!r}")
    return status


def _validate_kind(kind: str) -> str:
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"invalid ai_draft kind: {kind!r}")
    return kind


def _serialize_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        out = {col: row[col] for col in _COLUMNS if col in row}
    else:
        out = {_COLUMNS[i]: row[i] for i in range(min(len(_COLUMNS), len(row)))}
    for col in _JSON_COLS:
        val = out.get(col)
        if isinstance(val, (bytes, bytearray)):
            val = val.decode("utf-8", errors="replace")
        if isinstance(val, str):
            try:
                out[col] = json.loads(val)
            except Exception:
                out[col] = val
    for col in _TS_COLS:
        if col in out:
            out[col] = _iso(out[col])
    return out


def _cols() -> str:
    return ", ".join(_COLUMNS)


def insert_draft(
    conn: _Connection,
    *,
    kind: str,
    payload: Any,
    scope: str,
    generated_by: str,
    linked_action_id: str | None = None,
    status: str = "pending",
    expires_at: Any = None,
    draft_id: str | None = None,
    expire_prior_pending: bool = False,
) -> dict[str, Any]:
    did = (draft_id or generate_draft_id()).strip()
    validated_kind = _validate_kind(kind)
    validated_status = _validate_status(status)
    scope_s = str(scope).strip()
    if not scope_s:
        raise ValueError("scope is required")
    if not generated_by or not str(generated_by).strip():
        raise ValueError("generated_by is required")
    if payload is None:
        raise ValueError("payload is required")

    expire_sql = f"""
        UPDATE {TABLE_RESEARCH_AI_DRAFT}
        SET status = 'expired'
        WHERE kind = %s
          AND status = 'pending'
          AND id <> %s
          AND created_at < NOW()
    """
    insert_sql = f"""
        INSERT INTO {TABLE_RESEARCH_AI_DRAFT} (
            id, kind, payload, scope, status, generated_by,
            linked_action_id, expires_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING {_cols()}
    """
    insert_params = (
        did,
        validated_kind,
        _serialize_json(payload),
        scope_s,
        validated_status,
        str(generated_by).strip(),
        linked_action_id,
        expires_at,
    )
    try:
        with conn.cursor() as cur:
            if expire_prior_pending:
                # Same write transaction: older pending rows of this kind leave
                # the Inbox. Scope is not a match key — digest scope is one
                # calendar day, so yesterday's pending would never expire.
                cur.execute(expire_sql, (validated_kind, did))
            cur.execute(insert_sql, insert_params)
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row is None:
        raise RuntimeError("insert ai_draft returned no row")
    return _row_to_dict(row)


def get_draft(conn: _Connection, draft_id: str) -> dict[str, Any] | None:
    sql = f"""
        SELECT {_cols()}
        FROM {TABLE_RESEARCH_AI_DRAFT}
        WHERE id = %s
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (draft_id,))
        row = cur.fetchone()
    return _row_to_dict(row) if row is not None else None


def list_drafts(
    conn: _Connection,
    *,
    status: str | None = "pending",
    kind: str | None = None,
    scope: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        _validate_status(status)
        clauses.append("status = %s")
        params.append(status)
    if kind:
        _validate_kind(kind)
        clauses.append("kind = %s")
        params.append(kind)
    if scope:
        clauses.append("scope = %s")
        params.append(scope)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT {_cols()}
        FROM {TABLE_RESEARCH_AI_DRAFT}
        {where}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([int(limit), int(offset)])
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []
    return [_row_to_dict(r) for r in rows]


def count_pending(conn: _Connection, *, kind: str | None = None) -> int:
    clauses = ["status = 'pending'"]
    params: list[Any] = []
    if kind:
        _validate_kind(kind)
        clauses.append("kind = %s")
        params.append(kind)
    sql = f"""
        SELECT COUNT(*)
        FROM {TABLE_RESEARCH_AI_DRAFT}
        WHERE {' AND '.join(clauses)}
    """
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
    if row is None:
        return 0
    if isinstance(row, Mapping):
        return int(next(iter(row.values())))
    return int(row[0] or 0)


def count_pending_by_kind(conn: _Connection) -> dict[str, int]:
    """Pending rows per kind, counted in SQL — no page size, so no ceiling."""
    sql = f"""
        SELECT kind, COUNT(*)
        FROM {TABLE_RESEARCH_AI_DRAFT}
        WHERE status = 'pending'
        GROUP BY kind
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall() or []
    out: dict[str, int] = {}
    for row in rows:
        kind, n = (row["kind"], row["count"]) if isinstance(row, Mapping) else (row[0], row[1])
        out[str(kind)] = int(n or 0)
    return out


#: The Decision Inbox's card key (bifrost-trade-frontend
#: ``lib/harness/inboxCards.ts`` ``inboxCardKey``), in SQL. A value counts only
#: when it is a non-blank string, as ``nonEmpty`` there: a blank
#: ``hypothesis_id`` never merges calls, so it falls through to ``draft:<id>``.
_INBOX_CARD_KEY_SQL = """
    CASE
        WHEN a.kind IN ('decision_draft', 'order_intent') AND k.hyp IS NOT NULL
            THEN 'call:' || k.hyp
        WHEN a.kind = 'candidate_batch' AND k.obj IS NOT NULL
            THEN 'pool:' || k.obj
        WHEN a.kind = 'policy_suggestion' AND a.scope LIKE 'objective:%%' AND k.obj IS NOT NULL
            THEN 'patch:' || k.obj
        ELSE 'draft:' || a.id
    END
"""


def pending_inbox_cards(conn: _Connection, *, exclude_kinds: frozenset[str] | set[str]) -> list[dict[str, Any]]:
    """One row per Decision Inbox card over every pending draft not in ``exclude_kinds``.

    Grouped in SQL, so the count does not depend on how many rows a page holds:
    the earlier count read the newest 500 pending drafts of every kind, and with
    hundreds of briefings in the queue most of the calls never reached it.

    Each row carries the card key, how many drafts it holds (``n``), how many
    of them are verdicts / vehicles (for the call fold), and the newest draft's
    kind and scope. The payload comes back only for a ``policy_suggestion``
    head, the one kind whose payload decides whether Approve writes anything.
    """
    sql = f"""
        WITH d AS (
            SELECT
                a.id,
                a.kind,
                a.scope,
                a.payload,
                a.created_at,
                {_INBOX_CARD_KEY_SQL} AS card_key
            FROM {TABLE_RESEARCH_AI_DRAFT} a
            CROSS JOIN LATERAL (
                SELECT
                    CASE WHEN jsonb_typeof(a.payload -> 'hypothesis_id') = 'string'
                         THEN NULLIF(BTRIM(a.payload ->> 'hypothesis_id'), '') END AS hyp,
                    COALESCE(
                        CASE WHEN jsonb_typeof(a.payload -> 'objective_id') = 'string'
                             THEN NULLIF(BTRIM(a.payload ->> 'objective_id'), '') END,
                        CASE WHEN a.scope LIKE 'objective:%%'
                             THEN NULLIF(BTRIM(SUBSTRING(a.scope FROM 11)), '') END
                    ) AS obj
            ) k
            WHERE a.status = 'pending'
              AND NOT (a.kind = ANY(%s))
        )
        SELECT DISTINCT ON (card_key)
            card_key,
            COUNT(*) OVER w AS n,
            COUNT(*) FILTER (WHERE kind = 'decision_draft') OVER w AS verdicts,
            COUNT(*) FILTER (WHERE kind = 'order_intent') OVER w AS vehicles,
            kind,
            scope,
            CASE WHEN kind = 'policy_suggestion' THEN payload END AS payload
        FROM d
        WINDOW w AS (PARTITION BY card_key)
        ORDER BY card_key, created_at DESC, id DESC
    """
    cols = ("card_key", "n", "verdicts", "vehicles", "kind", "scope", "payload")
    with conn.cursor() as cur:
        cur.execute(sql, (sorted(exclude_kinds),))
        rows = cur.fetchall() or []
    out: list[dict[str, Any]] = []
    for row in rows:
        rec = {c: row[c] for c in cols} if isinstance(row, Mapping) else dict(zip(cols, row, strict=False))
        payload = rec.get("payload")
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", errors="replace")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = None
        rec["payload"] = payload
        for c in ("n", "verdicts", "vehicles"):
            rec[c] = int(rec.get(c) or 0)
        out.append(rec)
    return out


def update_draft_status(
    conn: _Connection,
    draft_id: str,
    *,
    status: str,
) -> dict[str, Any] | None:
    validated = _validate_status(status)
    sql = f"""
        UPDATE {TABLE_RESEARCH_AI_DRAFT}
        SET status = %s
        WHERE id = %s
        RETURNING {_cols()}
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (validated, draft_id))
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _row_to_dict(row) if row is not None else None


def patch_draft_payload(
    conn: _Connection,
    draft_id: str,
    payload: Any,
) -> dict[str, Any] | None:
    """Replace a draft's payload in place — the leash records who it accepted and why the rest stay."""
    sql = f"""
        UPDATE {TABLE_RESEARCH_AI_DRAFT}
        SET payload = %s
        WHERE id = %s
        RETURNING {_cols()}
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (_serialize_json(payload), draft_id))
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _row_to_dict(row) if row is not None else None

