"""When a pending ``research.ai_draft`` stops being a question (0.166.0).

Owner decisions D1–D8 (REQUEST-research-draft-expiry-2026-10-04): a pending
draft leaves the Inbox for one of three reasons, and each is recorded on the
row as ``payload.expired = {reason, by, at, ...}`` with ``status = 'expired'``
(no new column, D2):

- **superseded** — a newer pending draft with the same key arrived (written in
  the same transaction as that newer draft, by ``ai_draft.insert_draft``);
- **due** — ``expires_at`` passed (filled at write time from the kind's rule);
- **the subject closed** — the hypothesis or objective it is about is no longer
  active, or every name of a candidate batch has left ``open`` in the pool.

The last two are swept by ``expire_due`` (EOD review runs it first, D5).
Expiring never touches ``research.candidate_pool`` (D8): a batch's draft is
closed, its pool rows are left to the pool's own TTL. The linked
``ai_action_log`` row moves ``proposed`` → ``expired`` with it (D4).

The rules are pure functions of a row so the write path, the sweep and the
one-off backlog script decide exactly the same way.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from bifrost_research.schema.schemas import (
    TABLE_RESEARCH_AI_ACTION_LOG,
    TABLE_RESEARCH_AI_DRAFT,
    TABLE_RESEARCH_CANDIDATE_POOL,
    TABLE_RESEARCH_HYPOTHESIS,
    TABLE_RESEARCH_OBJECTIVE,
)

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")
_CLOSE_HOUR = 16  # NYSE regular close, America/New_York

REASON_SUPERSEDED = "superseded"
REASON_DUE = "due"
REASON_HYPOTHESIS_INACTIVE = "hypothesis_inactive"
REASON_HYPOTHESIS_MISSING = "hypothesis_missing"
REASON_OBJECTIVE_INACTIVE = "objective_inactive"
REASON_OBJECTIVE_MISSING = "objective_missing"
REASON_BATCH_CLOSED = "batch_closed"
REASON_MANUAL = "manual"

#: The order a sweep checks the reasons in; the first that holds is recorded.
REASONS = (
    REASON_SUPERSEDED,
    REASON_HYPOTHESIS_MISSING,
    REASON_HYPOTHESIS_INACTIVE,
    REASON_OBJECTIVE_MISSING,
    REASON_OBJECTIVE_INACTIVE,
    REASON_BATCH_CLOSED,
    REASON_DUE,
)

#: Kinds whose subject is a hypothesis: they close when it is no longer active.
HYPOTHESIS_KINDS = frozenset({"eod_verdict", "morning_brief", "decision_draft", "order_intent"})

WEEKLY_SOURCE = "weekly_outcomes"
DECISION_DRAFT_TRADING_DAYS = 10
ORDER_INTENT_TRADING_DAYS = 5
POLICY_SUGGESTION_DAYS = 7
HYPOTHESIS_DRAFT_DAYS = 14


# ─── row helpers ─────────────────────────────────────────────────────────────


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def as_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:  # noqa: BLE001
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_owner_authored(payload: Mapping[str, Any], generated_by: Any = None) -> bool:
    """A draft the Owner wrote by hand — the same test the approve path uses for
    policy_suggestion (``manual`` or ``source == 'owner'``), plus the author
    column (``owner:<id>`` from the harness API, ``owner_manual`` from the Inbox)."""
    if payload.get("manual") is True or payload.get("source") == "owner":
        return True
    return str(generated_by or "").strip().lower().startswith("owner")


def hypothesis_ref(kind: str, payload: Mapping[str, Any], scope: str) -> str | None:
    """The hypothesis a draft is about, or None.

    decision_draft / order_intent read ``payload.hypothesis_id`` only, as the
    Inbox does: a blank id never merges (and their scope is
    ``hypothesis:None`` when the id was None). eod_verdict / morning_brief fall
    back to the scope — a bare hypothesis id (EOD, resolution, Morning Prep) or
    ``hypothesis:<id>`` (validate hook, manual) — but never ``global`` or any
    other ``prefix:...`` scope.
    """
    hid = _text(payload.get("hypothesis_id"))
    if kind in ("decision_draft", "order_intent"):
        return hid
    if kind not in ("eod_verdict", "morning_brief"):
        return None
    if hid:
        return hid
    s = (scope or "").strip()
    if s.startswith("hypothesis:"):
        return _text(s[len("hypothesis:") :])
    if s and ":" not in s and s != "global":
        return s
    return None


def objective_ref(payload: Mapping[str, Any], scope: str) -> str | None:
    oid = _text(payload.get("objective_id"))
    if oid:
        return oid
    s = (scope or "").strip()
    if s.startswith("objective:"):
        return _text(s[len("objective:") :])
    return None


def supersede_key(kind: str, payload: Mapping[str, Any], scope: str) -> str | None:
    """Two pending drafts of one kind with the same key: the newer covers the older."""
    s = (scope or "").strip()
    if kind == "daily_digest":
        # One digest a day; the newest covers every earlier one (scope is the day).
        return "digest"
    if kind == "eod_verdict":
        hid = hypothesis_ref(kind, payload, s)
        return f"hyp:{hid}" if hid else None
    if kind == "morning_brief":
        hid = hypothesis_ref(kind, payload, s)
        if hid:
            return f"hyp:{hid}"
        return f"scope:{s}" if s else None
    if kind in ("decision_draft", "order_intent"):
        hid = hypothesis_ref(kind, payload, s)
        return f"hyp:{hid}" if hid else None
    if kind == "candidate_batch":
        oid = objective_ref(payload, s)
        return f"obj:{oid}" if oid else None
    if kind == "policy_suggestion":
        # Objective-level patches only (the Inbox folds the same set, Rev .143 #12).
        if not s.startswith("objective:"):
            return None
        oid = objective_ref(payload, s)
        return f"obj:{oid}" if oid else None
    return None


# ─── trading calendar ────────────────────────────────────────────────────────


_CLOSED_CACHE: dict[tuple[date, date], tuple[float, frozenset[date]]] = {}
_CLOSED_TTL_S = 3600.0


def closed_days(conn: Any, start: date, end: date) -> frozenset[date]:
    """NYSE weekday closures in ``[start, end]`` from ``db/calendar.py``.

    Read before any write of the caller's transaction (the calendar helper rolls
    back on a failed read). An unreadable calendar counts weekends only; a
    holiday then costs one trading day of lifetime, never a write.
    """
    hit = _CLOSED_CACHE.get((start, end))
    if hit is not None and time.monotonic() - hit[0] < _CLOSED_TTL_S:
        return hit[1]
    from bifrost_research.db.calendar import fetch_closed_holiday_dates

    try:
        days = frozenset(d for d in fetch_closed_holiday_dates(conn, start=start, end=end) if isinstance(d, date))
    except Exception as exc:  # noqa: BLE001
        logger.warning("draft expiry: trading calendar unreadable, weekends only: %s", str(exc)[:160])
        return frozenset()
    if days:
        _CLOSED_CACHE[(start, end)] = (time.monotonic(), days)
    return days


def _is_session(d: date, closed: frozenset[date] | set[date]) -> bool:
    return d.weekday() < 5 and d not in closed


def nth_session_after(d: date, n: int, closed: frozenset[date] | set[date]) -> date:
    """The ``n``-th trading day strictly after ``d``."""
    cur = d
    seen = 0
    while seen < n:
        cur += timedelta(days=1)
        if _is_session(cur, closed):
            seen += 1
    return cur


def session_close(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, _CLOSE_HOUR, tzinfo=_NY).astimezone(timezone.utc)


def _et_date(ts: datetime) -> date:
    return ts.astimezone(_NY).date()


def default_expires_at(
    kind: str,
    payload: Mapping[str, Any],
    created_at: datetime,
    closed: frozenset[date] | set[date],
) -> datetime | None:
    """``expires_at`` for a draft written at ``created_at`` (REQUEST §3). None = no expiry."""
    created = as_utc(created_at) or datetime.now(timezone.utc)
    day = _et_date(created)
    if kind == "eod_verdict":
        # The next EOD rewrites it; until then it is today's verdict.
        return session_close(nth_session_after(day, 1, closed))
    if kind == "morning_brief":
        # Good for the session it was written for: today's if it is still open.
        if _is_session(day, closed) and created < session_close(day):
            return session_close(day)
        return session_close(nth_session_after(day, 1, closed))
    if kind == "decision_draft":
        return session_close(nth_session_after(day, DECISION_DRAFT_TRADING_DAYS, closed))
    if kind == "order_intent":
        own = as_utc(payload.get("expiry_at"))
        if own is not None:
            return own
        return session_close(nth_session_after(day, ORDER_INTENT_TRADING_DAYS, closed))
    if kind == "policy_suggestion":
        if payload.get("source") == WEEKLY_SOURCE:
            # End of the next ISO week: the next weekly review lands before it.
            monday = day - timedelta(days=day.weekday())
            end = monday + timedelta(days=14)
            return datetime(end.year, end.month, end.day, tzinfo=_NY).astimezone(timezone.utc)
        return created + timedelta(days=POLICY_SUGGESTION_DAYS)
    if kind in ("hypothesis_draft", "hypothesis_suggestion"):
        return created + timedelta(days=HYPOTHESIS_DRAFT_DAYS)
    # daily_digest (covered by the next digest), candidate_batch (follows the
    # pool), playbook_rule / playbook_note (knowledge, D6): no clock.
    return None


def needs_calendar(kind: str, payload: Mapping[str, Any]) -> bool:
    if kind in ("eod_verdict", "morning_brief", "decision_draft"):
        return True
    return kind == "order_intent" and as_utc(payload.get("expiry_at")) is None


def expires_at_for(
    conn: Any,
    kind: str,
    payload: Mapping[str, Any],
    *,
    created_at: datetime,
) -> datetime | None:
    """The write path's ``expires_at``: the calendar is read only for the kinds that count sessions."""
    if needs_calendar(kind, payload):
        start = _et_date(created_at)
        closed = closed_days(conn, start, start + timedelta(days=45))
    else:
        closed = frozenset()
    return default_expires_at(kind, payload, created_at, closed)


# ─── the decision ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DraftRow:
    id: str
    kind: str
    scope: str
    payload: dict[str, Any]
    generated_by: str
    linked_action_id: str | None
    created_at: datetime
    expires_at: datetime | None

    @property
    def owner(self) -> bool:
        return is_owner_authored(self.payload, self.generated_by)


@dataclass(frozen=True)
class Expiry:
    id: str
    kind: str
    reason: str
    linked_action_id: str | None = None
    superseded_by: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def info(self, *, by: str, at: datetime) -> dict[str, Any]:
        out: dict[str, Any] = {"reason": self.reason, "by": by, "at": at.astimezone(timezone.utc).isoformat()}
        if self.superseded_by:
            out["superseded_by"] = self.superseded_by
        out.update(self.detail)
        return out


_ROW_COLS = ("id", "kind", "scope", "payload", "generated_by", "linked_action_id", "created_at", "expires_at")


def to_row(raw: Any) -> DraftRow:
    if isinstance(raw, DraftRow):
        return raw
    rec = {c: raw.get(c) for c in _ROW_COLS} if isinstance(raw, Mapping) else dict(zip(_ROW_COLS, raw, strict=False))
    return DraftRow(
        id=str(rec.get("id") or ""),
        kind=str(rec.get("kind") or ""),
        scope=str(rec.get("scope") or ""),
        payload=as_payload(rec.get("payload")),
        generated_by=str(rec.get("generated_by") or ""),
        linked_action_id=rec.get("linked_action_id") or None,
        created_at=as_utc(rec.get("created_at")) or datetime.fromtimestamp(0, timezone.utc),
        expires_at=as_utc(rec.get("expires_at")),
    )


def may_supersede(old: DraftRow, new: DraftRow) -> bool:
    """D3, applied to every kind: a model's draft never covers the Owner's own."""
    return not (old.owner and not new.owner)


def superseded_by_new(existing: Iterable[Any], new: DraftRow) -> list[Expiry]:
    """The write path: which pending rows the new draft covers."""
    key = supersede_key(new.kind, new.payload, new.scope)
    if key is None:
        return []
    out: list[Expiry] = []
    for raw in existing:
        old = to_row(raw)
        if old.id == new.id or old.kind != new.kind:
            continue
        if supersede_key(old.kind, old.payload, old.scope) != key or not may_supersede(old, new):
            continue
        out.append(Expiry(old.id, old.kind, REASON_SUPERSEDED, old.linked_action_id, superseded_by=new.id))
    return out


def batch_item_ids(payload: Mapping[str, Any]) -> list[str]:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    out: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            cid = _text(item.get("id")) or _text(item.get("candidate_id"))
            if cid:
                out.append(cid)
    return out


def plan_expiry(
    rows: Iterable[Any],
    *,
    now: datetime,
    closed: frozenset[date] | set[date] = frozenset(),
    hypothesis_status: Mapping[str, str] | None = None,
    objective_status: Mapping[str, str] | None = None,
    pool_status: Mapping[str, str] | None = None,
) -> list[Expiry]:
    """Every pending row that should be expired now, with the first reason that holds.

    A status map of None means "could not be read": that rule is skipped
    rather than reading every subject as missing.
    """
    pending = [to_row(r) for r in rows]
    decided: dict[str, Expiry] = {}

    # 1. superseded — newest per (kind, key); an Owner's draft only by a newer Owner draft.
    groups: dict[tuple[str, str], list[DraftRow]] = defaultdict(list)
    for r in pending:
        key = supersede_key(r.kind, r.payload, r.scope)
        if key is not None:
            groups[(r.kind, key)].append(r)
    for members in groups.values():
        members.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        newest_any: DraftRow | None = None
        newest_owner: DraftRow | None = None
        for r in members:
            head = newest_owner if r.owner else newest_any
            if head is not None:
                decided[r.id] = Expiry(r.id, r.kind, REASON_SUPERSEDED, r.linked_action_id, superseded_by=head.id)
            if newest_any is None:
                newest_any = r
            if r.owner and newest_owner is None:
                newest_owner = r

    for r in pending:
        if r.id in decided:
            continue
        # 2. the hypothesis it is about is gone or settled. A resolution notice
        #    (applied=True) reports that settlement; it runs out on its clock.
        if hypothesis_status is not None and r.kind in HYPOTHESIS_KINDS and r.payload.get("applied") is not True:
            hid = hypothesis_ref(r.kind, r.payload, r.scope)
            if hid:
                status = hypothesis_status.get(hid)
                if status is None:
                    decided[r.id] = Expiry(r.id, r.kind, REASON_HYPOTHESIS_MISSING, r.linked_action_id, detail={"hypothesis_id": hid})
                    continue
                if status != "active":
                    decided[r.id] = Expiry(
                        r.id,
                        r.kind,
                        REASON_HYPOTHESIS_INACTIVE,
                        r.linked_action_id,
                        detail={"hypothesis_id": hid, "hypothesis_status": status},
                    )
                    continue
        # 3. the objective it is about is archived or gone.
        if objective_status is not None and (
            r.kind == "candidate_batch" or (r.kind == "policy_suggestion" and r.scope.startswith("objective:"))
        ):
            oid = objective_ref(r.payload, r.scope)
            if oid:
                status = objective_status.get(oid)
                if status is None:
                    decided[r.id] = Expiry(r.id, r.kind, REASON_OBJECTIVE_MISSING, r.linked_action_id, detail={"objective_id": oid})
                    continue
                if status != "active":
                    decided[r.id] = Expiry(
                        r.id,
                        r.kind,
                        REASON_OBJECTIVE_INACTIVE,
                        r.linked_action_id,
                        detail={"objective_id": oid, "objective_status": status},
                    )
                    continue
        # 4. a batch with no name still open in the pool: Approve would promote nothing.
        if pool_status is not None and r.kind == "candidate_batch":
            ids = batch_item_ids(r.payload)
            if not any(pool_status.get(cid) == "open" for cid in ids):
                states = Counter(pool_status.get(cid) or "missing" for cid in ids)
                decided[r.id] = Expiry(
                    r.id, r.kind, REASON_BATCH_CLOSED, r.linked_action_id, detail={"pool": dict(sorted(states.items()))}
                )
                continue
        # 5. its clock ran out. Rows written before 0.166.0 carry no expires_at;
        #    their kind's rule is applied from created_at.
        due = r.expires_at
        defaulted = False
        if due is None:
            due = default_expires_at(r.kind, r.payload, r.created_at, closed)
            defaulted = due is not None
        if due is not None and due <= now:
            detail: dict[str, Any] = {"expires_at": due.isoformat()}
            if defaulted:
                detail["expires_at_defaulted"] = True
            decided[r.id] = Expiry(r.id, r.kind, REASON_DUE, r.linked_action_id, detail=detail)

    order = {r.id: i for i, r in enumerate(pending)}
    return sorted(decided.values(), key=lambda e: order.get(e.id, 0))


# ─── reads ───────────────────────────────────────────────────────────────────


def _fetchall(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall() or [])


def pending_rows(conn: Any) -> list[DraftRow]:
    rows = _fetchall(
        conn,
        f"""
        SELECT {", ".join(_ROW_COLS)}
        FROM {TABLE_RESEARCH_AI_DRAFT}
        WHERE status = 'pending'
        ORDER BY created_at, id
        """,
    )
    return [to_row(r) for r in rows]


def _status_map(conn: Any, table: str, ids: set[str]) -> dict[str, str] | None:
    if not ids:
        return {}
    try:
        rows = _fetchall(conn, f"SELECT id, status FROM {table} WHERE id = ANY(%s)", (sorted(ids),))
    except Exception as exc:  # noqa: BLE001
        logger.warning("draft expiry: %s unreadable, rule skipped: %s", table, str(exc)[:160])
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    out: dict[str, str] = {}
    for row in rows:
        rid, status = (row["id"], row["status"]) if isinstance(row, Mapping) else (row[0], row[1])
        out[str(rid)] = str(status)
    return out


def collect_plan(
    conn: Any,
    *,
    now: datetime | None = None,
    strict: bool = False,
) -> tuple[list[Expiry], dict[str, int]]:
    """Read what the rules need (pending drafts, subject statuses, pool, calendar) and plan.

    Read-only. Returns the plan and the pending count per kind it was taken over.
    ``strict`` raises instead of skipping a rule whose subject table could not
    be read (the one-off backlog script must not under-count silently).
    """
    now = as_utc(now) or datetime.now(timezone.utc)
    rows = pending_rows(conn)
    hyp_ids = {
        h
        for r in rows
        if r.kind in HYPOTHESIS_KINDS and (h := hypothesis_ref(r.kind, r.payload, r.scope))
    }
    obj_ids = {
        o
        for r in rows
        if (r.kind == "candidate_batch" or (r.kind == "policy_suggestion" and r.scope.startswith("objective:")))
        and (o := objective_ref(r.payload, r.scope))
    }
    pool_ids = {cid for r in rows if r.kind == "candidate_batch" for cid in batch_item_ids(r.payload)}
    hyp_status = _status_map(conn, TABLE_RESEARCH_HYPOTHESIS, hyp_ids)
    obj_status = _status_map(conn, TABLE_RESEARCH_OBJECTIVE, obj_ids)
    # Read, never written (D8).
    pool_status = _status_map(conn, TABLE_RESEARCH_CANDIDATE_POOL, pool_ids)
    if strict and None in (hyp_status, obj_status, pool_status):
        raise RuntimeError("draft expiry: a subject table could not be read; plan would under-count")
    if any(r.expires_at is None and needs_calendar(r.kind, r.payload) for r in rows):
        start = min(_et_date(r.created_at) for r in rows)
        closed = closed_days(conn, start, max(start, _et_date(now)) + timedelta(days=45))
    else:
        closed = frozenset()
    plan = plan_expiry(
        rows,
        now=now,
        closed=closed,
        hypothesis_status=hyp_status,
        objective_status=obj_status,
        pool_status=pool_status,
    )
    return plan, dict(sorted(Counter(r.kind for r in rows).items()))


# ─── writes (research.ai_draft and research.ai_action_log only) ──────────────


_APPLY_SQL = f"""
    UPDATE {TABLE_RESEARCH_AI_DRAFT} d
    SET status = 'expired',
        payload = CASE
            WHEN jsonb_typeof(d.payload) = 'object'
                THEN d.payload || jsonb_build_object('expired', u.info::jsonb)
            ELSE d.payload
        END
    FROM unnest(%s::text[], %s::text[]) AS u(id, info)
    WHERE d.id = u.id
      AND d.status = 'pending'
    RETURNING d.id, d.kind, d.linked_action_id
"""

_ACTION_SQL = f"""
    UPDATE {TABLE_RESEARCH_AI_ACTION_LOG}
    SET status = 'expired'
    WHERE id = ANY(%s)
      AND status = 'proposed'
"""


def apply_with_cursor(cur: Any, plan: list[Expiry], *, by: str, at: datetime) -> dict[str, Any]:
    """Expire the planned rows inside the caller's transaction (no commit).

    Guarded on ``status = 'pending'``, so a row decided in between is left
    alone and a second run changes nothing. Only the linked action rows still
    ``proposed`` move to ``expired``; an executed resolution notice stays executed.
    """
    if not plan:
        return {"expired": 0, "actions_expired": 0, "ids": []}
    ids = [e.id for e in plan]
    infos = [json.dumps(e.info(by=by, at=at), default=str) for e in plan]
    cur.execute(_APPLY_SQL, (ids, infos))
    changed = list(cur.fetchall() or [])
    done_ids: list[str] = []
    action_ids: list[str] = []
    for row in changed:
        rid, _kind, aid = (row["id"], row["kind"], row["linked_action_id"]) if isinstance(row, Mapping) else row[:3]
        done_ids.append(str(rid))
        if aid:
            action_ids.append(str(aid))
    actions = 0
    if action_ids:
        cur.execute(_ACTION_SQL, (action_ids,))
        actions = int(getattr(cur, "rowcount", 0) or 0)
    return {"expired": len(done_ids), "actions_expired": actions, "ids": done_ids}


def apply_expiry(
    conn: Any,
    plan: list[Expiry],
    *,
    by: str,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    at = as_utc(now) or datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            out = apply_with_cursor(cur, plan, by=by, at=at)
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return out


def summarize(plan: list[Expiry]) -> dict[str, Any]:
    by_kind: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in plan:
        by_kind[e.kind][e.reason] += 1
    return {
        "total": len(plan),
        "by_reason": dict(sorted(Counter(e.reason for e in plan).items())),
        "by_kind": {k: dict(sorted(v.items())) for k, v in sorted(by_kind.items())},
    }


def expire_due(
    conn: Any,
    *,
    now: datetime | None = None,
    by: str = "expire_due",
    dry_run: bool = False,
) -> dict[str, Any]:
    """The sweep (D1/D5): expire every pending draft whose rule now holds.

    Idempotent — a second run finds nothing left to expire.
    """
    at = as_utc(now) or datetime.now(timezone.utc)
    plan, pending_by_kind = collect_plan(conn, now=at)
    out: dict[str, Any] = {"ok": True, "dry_run": dry_run, "pending_by_kind": pending_by_kind, **summarize(plan)}
    if dry_run:
        out["expired"] = 0
        out["actions_expired"] = 0
        return out
    applied = apply_expiry(conn, plan, by=by, now=at)
    out["expired"] = applied["expired"]
    out["actions_expired"] = applied["actions_expired"]
    return out


def expire_ids(
    conn: Any,
    draft_ids: list[str],
    *,
    reason: str,
    by: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Expire named pending drafts (manual: ``POST /research/order-intents/{id}/expire``)."""
    rows = _fetchall(
        conn,
        f"SELECT id, kind, linked_action_id FROM {TABLE_RESEARCH_AI_DRAFT} WHERE id = ANY(%s) AND status = 'pending'",
        (list(draft_ids),),
    )
    plan = []
    for row in rows:
        rid, kind, aid = (row["id"], row["kind"], row["linked_action_id"]) if isinstance(row, Mapping) else row[:3]
        plan.append(Expiry(str(rid), str(kind), reason, aid or None))
    return apply_expiry(conn, plan, by=by, now=now)
