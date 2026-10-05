"""SQL layer for ``research.ai_draft`` — Wave RS-E3 Cockpit inbox (D-RS-E-e)."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from bifrost_research.repositories import draft_expiry as expiry
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


#: A pending row a reader may still see: not past its ``expires_at``. The sweep
#: (``expire_due``) turns these into ``status = 'expired'``; between two sweeps
#: this keeps a row that ran out from being counted, listed or approved.
LIVE_SQL = "(expires_at IS NULL OR expires_at > now())"


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
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write a draft — every writer comes through here (0.166.0, D1).

    A pending draft gets its kind's ``expires_at`` when the caller did not give
    one (``draft_expiry.default_expires_at``), and in the same transaction
    expires the older pending drafts it covers (same kind, same key —
    ``draft_expiry.supersede_key``; an Owner-authored draft only by a newer
    Owner-authored one, D3). Those rows record ``payload.expired`` with
    ``superseded_by`` and their ``proposed`` action rows become ``expired`` (D4).
    """
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
    author = str(generated_by).strip()
    at = expiry.as_utc(now) or datetime.now(timezone.utc)
    body = expiry.as_payload(payload)

    key: str | None = None
    if validated_status == "pending":
        if expires_at is None:
            # Read before the write transaction opens: the calendar helper rolls
            # back on a failed read.
            expires_at = expiry.expires_at_for(conn, validated_kind, body, created_at=at)
        key = expiry.supersede_key(validated_kind, body, scope_s)

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
        author,
        linked_action_id,
        expires_at,
    )
    try:
        with conn.cursor() as cur:
            if key is not None:
                sql, params = _supersede_candidates_sql(validated_kind, key, did)
                cur.execute(sql, params)
                existing = list(cur.fetchall() or [])
                new_row = expiry.DraftRow(
                    id=did,
                    kind=validated_kind,
                    scope=scope_s,
                    payload=body,
                    generated_by=author,
                    linked_action_id=linked_action_id,
                    created_at=at,
                    expires_at=expiry.as_utc(expires_at),
                )
                covered = expiry.superseded_by_new(existing, new_row)
                expiry.apply_with_cursor(cur, covered, by=f"insert:{author}", at=at)
            cur.execute(insert_sql, insert_params)
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row is None:
        raise RuntimeError("insert ai_draft returned no row")
    return _row_to_dict(row)


def _supersede_candidates_sql(kind: str, key: str, exclude_id: str) -> tuple[str, tuple[Any, ...]]:
    """Pending rows that may share ``key`` — narrowed in SQL, decided by ``draft_expiry``.

    ``FOR UPDATE`` so two writers covering the same key take turns rather
    than each expiring the other's row.
    """
    clauses = ["kind = %s", "status = 'pending'", "id <> %s"]
    params: list[Any] = [kind, exclude_id]
    tag, _, value = key.partition(":")
    if tag == "hyp":
        clauses.append("(BTRIM(payload ->> 'hypothesis_id') = %s OR scope IN (%s, %s))")
        params.extend([value, value, f"hypothesis:{value}"])
    elif tag == "obj":
        clauses.append("(BTRIM(payload ->> 'objective_id') = %s OR scope = %s)")
        params.extend([value, f"objective:{value}"])
    elif tag == "scope":
        clauses.append("scope = %s")
        params.append(value)
    sql = f"""
        SELECT {", ".join(expiry._ROW_COLS)}
        FROM {TABLE_RESEARCH_AI_DRAFT}
        WHERE {" AND ".join(clauses)}
        FOR UPDATE
    """
    return sql, tuple(params)


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
        if status == "pending":
            clauses.append(LIVE_SQL)
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
    clauses = ["status = 'pending'", LIVE_SQL]
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
          AND {LIVE_SQL}
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
              AND (a.expires_at IS NULL OR a.expires_at > now())
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



def expired_state(draft: Mapping[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Why this draft can no longer be approved or dismissed, or None while it still can.

    The 409 body of approve / dismiss (``detail``): ``code`` is always
    ``draft_expired``; ``reason`` is the recorded one (``superseded``, ``due``,
    ``hypothesis_inactive`` …) or ``due`` for a pending row past ``expires_at``
    that the sweep has not reached yet.
    """
    if not draft:
        return None
    status = draft.get("status")
    at = expiry.as_utc(now) or datetime.now(timezone.utc)
    payload = draft.get("payload") if isinstance(draft.get("payload"), Mapping) else {}
    record = payload.get("expired") if isinstance(payload.get("expired"), Mapping) else {}
    if status == "expired":
        reason = str(record.get("reason") or "expired")
        expired_at = record.get("at")
    elif status == "pending":
        due = expiry.as_utc(draft.get("expires_at"))
        if due is None or due > at:
            return None
        reason = expiry.REASON_DUE
        expired_at = due.isoformat()
    else:
        return None
    out: dict[str, Any] = {
        "code": "draft_expired",
        "draft_id": draft.get("id"),
        "kind": draft.get("kind"),
        "reason": reason,
        "expired_at": expired_at,
        "expires_at": _iso(draft.get("expires_at")),
        "superseded_by": record.get("superseded_by"),
        "message": f"draft expired ({reason}); it can no longer be approved or dismissed",
    }
    return out


def expire_due(conn: _Connection, *, now: datetime | None = None, by: str = "expire_due", dry_run: bool = False) -> dict[str, Any]:
    """Sweep pending drafts whose rule holds now — see ``draft_expiry.expire_due``."""
    return expiry.expire_due(conn, now=now, by=by, dry_run=dry_run)
