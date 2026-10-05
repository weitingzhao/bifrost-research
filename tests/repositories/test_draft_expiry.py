"""Pending drafts expire (0.166.0, REQUEST-research-draft-expiry D1–D8).

Three ways out of the Inbox, each recorded as ``status = 'expired'`` plus
``payload.expired = {reason, by, at, ...}`` (no new column, D2):

- a newer pending draft with the same key covers the older one, in the same
  transaction as the newer draft's INSERT (``insert_draft``);
- ``expires_at`` passes — filled at write time from the kind's rule;
- the subject closes: the hypothesis / objective is no longer active, or no
  name of a candidate batch is still ``open`` in the pool.

The linked ``ai_action_log`` row goes ``proposed`` → ``expired`` with it (D4);
``research.candidate_pool`` is read and never written (D8); the Owner's own
policy_suggestion is not covered by a model's (D3).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import draft_expiry as ex

NY = ZoneInfo("America/New_York")


def et(y: int, m: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=NY).astimezone(timezone.utc)


def _q(sql: str) -> str:
    return " ".join(sql.split()).lower()


class _Cursor:
    def __init__(self, db: "FakeDB") -> None:
        self.db = db
        self._rows: list[Any] = []
        self._one: Any = None
        self.rowcount = 0

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any:
        return self._one

    def execute(self, sql: str, params: Any = None) -> None:  # noqa: C901
        q = _q(sql)
        db = self.db
        db.statements.append(q)
        self._rows, self._one, self.rowcount = [], None, 0
        if "candidate_pool" in q and not q.startswith("select"):
            db.pool_writes.append(q)
        if "raw_market." in q:
            return
        if q.startswith("select id, status from research.hypothesis"):
            self._rows = [(i, db.hypotheses[i]) for i in params[0] if i in db.hypotheses]
            return
        if q.startswith("select id, status from research.objective"):
            self._rows = [(i, db.objectives[i]) for i in params[0] if i in db.objectives]
            return
        if q.startswith("select id, status from research.candidate_pool"):
            self._rows = [(i, db.pool[i]) for i in params[0] if i in db.pool]
            return
        if q.startswith("select id, kind, scope, payload") and "for update" in q:
            kind, exclude = params[0], params[1]
            rest = params[2:]
            out = []
            for r in db.pending():
                if r["kind"] != kind or r["id"] == exclude:
                    continue
                p = r["payload"]
                if "hypothesis_id" in q and len(rest) == 3:
                    if not (str(p.get("hypothesis_id") or "").strip() == rest[0] or r["scope"] in rest[1:]):
                        continue
                elif "objective_id" in q and len(rest) == 2:
                    if not (str(p.get("objective_id") or "").strip() == rest[0] or r["scope"] == rest[1]):
                        continue
                elif len(rest) == 1 and r["scope"] != rest[0]:
                    continue
                out.append(db.row_tuple(r, ex._ROW_COLS))
            self._rows = out
            return
        if q.startswith("select id, kind, scope, payload") and "where status = 'pending'" in q:
            self._rows = [db.row_tuple(r, ex._ROW_COLS) for r in sorted(db.pending(), key=lambda r: (r["created_at"], r["id"]))]
            return
        if q.startswith("select id, kind, linked_action_id from research.ai_draft"):
            ids = params[0]
            self._rows = [(r["id"], r["kind"], r["linked_action_id"]) for r in db.pending() if r["id"] in ids]
            return
        if q.startswith("update research.ai_draft d set status = 'expired'"):
            ids, infos = params
            out = []
            for did, info in zip(ids, infos, strict=True):
                r = db.drafts.get(did)
                if r is None or r["status"] != "pending":
                    continue
                r["status"] = "expired"
                r["payload"] = {**r["payload"], "expired": json.loads(info)}
                out.append((r["id"], r["kind"], r["linked_action_id"]))
            self._rows = out
            self.rowcount = len(out)
            return
        if q.startswith("update research.ai_action_log set status = 'expired'"):
            n = 0
            for aid in params[0]:
                a = db.actions.get(aid)
                if a is not None and a["status"] == "proposed":
                    a["status"] = "expired"
                    n += 1
            self.rowcount = n
            return
        if q.startswith("insert into research.ai_draft"):
            row = {
                "id": params[0],
                "kind": params[1],
                "payload": json.loads(params[2]) if isinstance(params[2], str) else params[2],
                "scope": params[3],
                "status": params[4],
                "generated_by": params[5],
                "linked_action_id": params[6],
                "created_at": db.clock,
                "expires_at": params[7],
            }
            db.drafts[row["id"]] = row
            self._one = db.row_tuple(row, draft_repo._COLUMNS)
            return
        if q.startswith(("select", "with")) and "research.ai_draft" in q:
            # list / count readers: record only.
            self._rows = []
            self._one = (0,)
            return
        raise AssertionError(f"unexpected SQL: {q[:120]}")


class FakeDB:
    def __init__(self, clock: datetime) -> None:
        self.clock = clock
        self.drafts: dict[str, dict[str, Any]] = {}
        self.actions: dict[str, dict[str, Any]] = {}
        self.hypotheses: dict[str, str] = {}
        self.objectives: dict[str, str] = {}
        self.pool: dict[str, str] = {}
        self.statements: list[str] = []
        self.pool_writes: list[str] = []
        self.commits = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None

    def pending(self) -> list[dict[str, Any]]:
        return [r for r in self.drafts.values() if r["status"] == "pending"]

    @staticmethod
    def row_tuple(r: dict[str, Any], cols: tuple[str, ...]) -> tuple[Any, ...]:
        out = []
        for c in cols:
            v = r.get(c)
            out.append(json.dumps(v) if c == "payload" else v)
        return tuple(out)

    def add(
        self,
        did: str,
        kind: str,
        *,
        payload: dict[str, Any] | None = None,
        scope: str = "global",
        created_at: datetime,
        generated_by: str = "agent",
        expires_at: datetime | None = None,
        status: str = "pending",
        action: str | None = "proposed",
    ) -> dict[str, Any]:
        aid = f"aal_{did}" if action else None
        if aid:
            self.actions[aid] = {"id": aid, "status": action}
        row = {
            "id": did,
            "kind": kind,
            "payload": payload or {},
            "scope": scope,
            "status": status,
            "generated_by": generated_by,
            "linked_action_id": aid,
            "created_at": created_at,
            "expires_at": expires_at,
        }
        self.drafts[did] = row
        return row


FRI = et(2026, 10, 2, 17, 30)  # an EOD on Friday 2026-10-02
MON_EOD = et(2026, 10, 5, 17, 30)


def _insert(db: FakeDB, kind: str, *, payload: dict[str, Any], scope: str, generated_by: str = "agent", **k: Any) -> dict[str, Any]:
    return draft_repo.insert_draft(
        db, kind=kind, payload=payload, scope=scope, generated_by=generated_by, now=db.clock, **k
    )


# ─── write path: supersede in the INSERT's transaction ───────────────────────


def test_eod_verdict_covers_older_verdicts_of_the_same_hypothesis() -> None:
    db = FakeDB(MON_EOD)
    db.add("v_bare", "eod_verdict", payload={"hypothesis_id": "hyp_a"}, scope="hyp_a", created_at=FRI)
    db.add("v_hook", "eod_verdict", payload={"hypothesis_id": "hyp_a"}, scope="hypothesis:hyp_a", created_at=FRI)
    db.add("v_other", "eod_verdict", payload={"hypothesis_id": "hyp_b"}, scope="hyp_b", created_at=FRI)
    db.add("v_notice", "eod_verdict", payload={"hypothesis_id": "hyp_a", "applied": True}, scope="hyp_a", created_at=FRI, action="executed")

    new = _insert(db, "eod_verdict", payload={"hypothesis_id": "hyp_a"}, scope="hyp_a", generated_by="eod_agent")

    for did in ("v_bare", "v_hook", "v_notice"):
        row = db.drafts[did]
        assert row["status"] == "expired"
        info = row["payload"]["expired"]
        assert info["reason"] == "superseded"
        assert info["superseded_by"] == new["id"]
        assert info["by"] == "insert:eod_agent"
    assert db.drafts["v_other"]["status"] == "pending"
    # D4: proposed action rows follow; an executed one (resolution notice) stays executed.
    assert db.actions["aal_v_bare"]["status"] == "expired"
    assert db.actions["aal_v_hook"]["status"] == "expired"
    assert db.actions["aal_v_notice"]["status"] == "executed"
    assert db.actions["aal_v_other"]["status"] == "proposed"
    # One transaction: supersede, then INSERT, then a single commit.
    upd = next(i for i, s in enumerate(db.statements) if s.startswith("update research.ai_draft d"))
    ins = next(i for i, s in enumerate(db.statements) if s.startswith("insert into research.ai_draft"))
    assert upd < ins
    assert db.commits == 1
    # Written with its clock: the next session's close (Tuesday 10-06, 16:00 ET).
    assert new["expires_at"] == et(2026, 10, 6, 16).isoformat()


def test_morning_brief_covers_by_scope() -> None:
    db = FakeDB(et(2026, 10, 5, 8))
    db.add("b_glob", "morning_brief", scope="global", created_at=FRI)
    db.add("b_hyp", "morning_brief", payload={"hypothesis_id": "hyp_a"}, scope="hyp_a", created_at=FRI)
    new = _insert(db, "morning_brief", payload={"title": "x"}, scope="global")
    assert db.drafts["b_glob"]["status"] == "expired"
    assert db.drafts["b_hyp"]["status"] == "pending"
    # Before the open: good for today's session.
    assert new["expires_at"] == et(2026, 10, 5, 16).isoformat()


def test_candidate_batch_covers_by_objective_and_never_writes_the_pool() -> None:
    db = FakeDB(MON_EOD)
    db.pool = {"c1": "open", "c2": "open"}
    db.add("cb_old", "candidate_batch", payload={"objective_id": "obj_a", "items": [{"id": "c1"}]}, scope="objective:obj_a", created_at=FRI)
    db.add("cb_other", "candidate_batch", payload={"objective_id": "obj_b", "items": [{"id": "c2"}]}, scope="objective:obj_b", created_at=FRI)
    new = _insert(db, "candidate_batch", payload={"objective_id": "obj_a", "items": [{"id": "c2"}]}, scope="objective:obj_a", generated_by="harness")
    assert db.drafts["cb_old"]["status"] == "expired"
    assert db.drafts["cb_other"]["status"] == "pending"
    assert new["expires_at"] is None  # follows the pool, no clock
    assert db.pool == {"c1": "open", "c2": "open"}
    assert db.pool_writes == []


def test_model_policy_suggestion_does_not_cover_the_owners() -> None:
    db = FakeDB(MON_EOD)
    owner = {"objective_id": "obj_a", "suggestion": {"x": 1}, "manual": True}
    db.add("ps_owner", "policy_suggestion", payload=owner, scope="objective:obj_a", created_at=FRI - timedelta(days=1), generated_by="owner:alice", action=None)
    db.add("ps_model", "policy_suggestion", payload={"objective_id": "obj_a", "source": "harness_llm_plan"}, scope="objective:obj_a", created_at=FRI)
    db.add("ps_loose", "policy_suggestion", payload={"source": "harness_llm_plan"}, scope="global", created_at=FRI)

    _insert(db, "policy_suggestion", payload={"objective_id": "obj_a", "source": "persona_eval_outcomes"}, scope="objective:obj_a", generated_by="harness")
    assert db.drafts["ps_model"]["status"] == "expired"
    assert db.drafts["ps_owner"]["status"] == "pending"  # D3
    assert db.drafts["ps_loose"]["status"] == "pending"  # not objective-level: no key

    newest_owner = _insert(db, "policy_suggestion", payload={"objective_id": "obj_a", "suggestion": {"x": 2}, "manual": True}, scope="objective:obj_a", generated_by="owner:alice")
    assert db.drafts["ps_owner"]["status"] == "expired"
    assert db.drafts["ps_owner"]["payload"]["expired"]["superseded_by"] == newest_owner["id"]


def test_calls_cover_by_hypothesis_and_kind_and_blank_ids_never_merge() -> None:
    db = FakeDB(MON_EOD)
    db.add("dd_a", "decision_draft", payload={"hypothesis_id": "hyp_a"}, scope="hypothesis:hyp_a", created_at=FRI)
    db.add("oi_a", "order_intent", payload={"hypothesis_id": "hyp_a"}, scope="hypothesis:hyp_a", created_at=FRI)
    db.add("dd_none", "decision_draft", payload={"hypothesis_id": None}, scope="hypothesis:None", created_at=FRI)
    _insert(db, "decision_draft", payload={"hypothesis_id": "hyp_a"}, scope="hypothesis:hyp_a", generated_by="loop_curator")
    assert db.drafts["dd_a"]["status"] == "expired"
    assert db.drafts["oi_a"]["status"] == "pending"  # other kind
    _insert(db, "decision_draft", payload={"hypothesis_id": None}, scope="hypothesis:None", generated_by="loop_curator")
    assert db.drafts["dd_none"]["status"] == "pending"


def test_daily_digest_covers_every_earlier_digest() -> None:
    db = FakeDB(et(2026, 10, 5, 7, 30))
    db.add("dg_old", "daily_digest", scope="digest:2026-10-02", created_at=FRI)
    db.add("other", "eod_verdict", payload={"hypothesis_id": "hyp_a"}, scope="hyp_a", created_at=FRI)
    new = _insert(db, "daily_digest", payload={"markdown": "# today"}, scope="digest:2026-10-05", generated_by="digest_agent")
    assert db.drafts["dg_old"]["status"] == "expired"
    assert db.drafts["dg_old"]["payload"]["expired"]["superseded_by"] == new["id"]
    assert db.actions["aal_dg_old"]["status"] == "expired"
    assert db.drafts["other"]["status"] == "pending"


def test_playbook_note_neither_covers_nor_expires() -> None:
    db = FakeDB(MON_EOD)
    db.add("pn_old", "playbook_note", scope="playbook", created_at=FRI)
    new = _insert(db, "playbook_note", payload={"note_md": "x"}, scope="playbook", generated_by="curator_agent")
    assert db.drafts["pn_old"]["status"] == "pending"
    assert new["expires_at"] is None
    assert not any(s.startswith("update") for s in db.statements)


def test_order_intent_keeps_its_own_expiry_or_gets_five_sessions() -> None:
    db = FakeDB(MON_EOD)
    own = et(2026, 10, 7, 12)
    a = _insert(db, "order_intent", payload={"hypothesis_id": "hyp_a"}, scope="hypothesis:hyp_a", expires_at=own)
    assert a["expires_at"] == own.isoformat()
    b = _insert(db, "order_intent", payload={"hypothesis_id": "hyp_b", "expiry_at": None}, scope="hypothesis:hyp_b")
    assert b["expires_at"] == et(2026, 10, 12, 16).isoformat()


def test_a_non_pending_insert_is_left_alone() -> None:
    db = FakeDB(MON_EOD)
    db.add("v", "eod_verdict", payload={"hypothesis_id": "hyp_a"}, scope="hyp_a", created_at=FRI)
    row = _insert(db, "eod_verdict", payload={"hypothesis_id": "hyp_a"}, scope="hyp_a", status="approved")
    assert row["expires_at"] is None
    assert db.drafts["v"]["status"] == "pending"


# ─── the clocks ──────────────────────────────────────────────────────────────


def test_default_expiry_per_kind() -> None:
    closed: set[date] = set()
    assert ex.default_expires_at("eod_verdict", {}, FRI, closed) == et(2026, 10, 5, 16)
    # A Monday holiday moves it a session.
    assert ex.default_expires_at("eod_verdict", {}, FRI, {date(2026, 10, 5)}) == et(2026, 10, 6, 16)
    # morning_brief after the close counts the next session.
    assert ex.default_expires_at("morning_brief", {}, et(2026, 10, 2, 18), closed) == et(2026, 10, 5, 16)
    assert ex.default_expires_at("morning_brief", {}, et(2026, 10, 3, 9), closed) == et(2026, 10, 5, 16)
    assert ex.default_expires_at("decision_draft", {}, FRI, closed) == et(2026, 10, 16, 16)
    assert ex.default_expires_at("order_intent", {}, FRI, closed) == et(2026, 10, 9, 16)
    assert ex.default_expires_at("order_intent", {"expiry_at": "2026-10-03T12:00:00Z"}, FRI, closed) == datetime(
        2026, 10, 3, 12, tzinfo=timezone.utc
    )
    # Weekly (W40, written Sunday 10-04): end of the next ISO week, Monday 10-12 00:00 ET.
    sunday = et(2026, 10, 4, 18)
    assert ex.default_expires_at("policy_suggestion", {"source": "weekly_outcomes"}, sunday, closed) == et(2026, 10, 12)
    assert ex.default_expires_at("policy_suggestion", {"source": "harness_llm_plan"}, FRI, closed) == FRI + timedelta(days=7)
    assert ex.default_expires_at("policy_suggestion", {"manual": True}, FRI, closed) == FRI + timedelta(days=7)
    assert ex.default_expires_at("hypothesis_suggestion", {}, FRI, closed) == FRI + timedelta(days=14)
    for kind in ("playbook_note", "playbook_rule", "candidate_batch", "daily_digest"):
        assert ex.default_expires_at(kind, {}, FRI, closed) is None


# ─── the sweep ───────────────────────────────────────────────────────────────


def _row(did: str, kind: str, created: datetime, *, payload: dict[str, Any] | None = None, scope: str = "global", by: str = "agent", expires: datetime | None = None) -> ex.DraftRow:
    return ex.DraftRow(did, kind, scope, payload or {}, by, f"aal_{did}", created, expires)


def test_plan_reasons() -> None:
    now = et(2026, 10, 5, 12)  # Monday midday: Friday's verdicts are still today's
    rows = [
        _row("v1", "eod_verdict", FRI - timedelta(days=1), payload={"hypothesis_id": "hyp_a"}, scope="hyp_a"),
        _row("v2", "eod_verdict", FRI, payload={"hypothesis_id": "hyp_a"}, scope="hyp_a"),
        _row("v_val", "eod_verdict", FRI, payload={"hypothesis_id": "hyp_v"}, scope="hyp_v"),
        _row("v_notice", "eod_verdict", FRI, payload={"hypothesis_id": "hyp_w", "applied": True}, scope="hyp_w"),
        _row("dd_gone", "decision_draft", FRI, payload={"hypothesis_id": "hyp_gone"}, scope="hypothesis:hyp_gone"),
        _row("cb_arch", "candidate_batch", FRI, payload={"objective_id": "obj_arch", "items": [{"id": "c_open"}]}, scope="objective:obj_arch"),
        _row("cb_closed", "candidate_batch", FRI, payload={"objective_id": "obj_a", "items": [{"id": "c_exp"}, {"id": "c_gone"}]}, scope="objective:obj_a"),
        _row("cb_live", "candidate_batch", FRI, payload={"objective_id": "obj_b", "items": [{"id": "c_exp"}, {"id": "c_open"}]}, scope="objective:obj_b"),
        _row("oi_due", "order_intent", FRI, payload={"hypothesis_id": "hyp_a"}, scope="hypothesis:hyp_a", expires=now - timedelta(minutes=1)),
        _row("dd_old", "decision_draft", et(2026, 9, 17, 12), payload={"hypothesis_id": "hyp_a"}, scope="hypothesis:hyp_a"),
        _row("ps_owner", "policy_suggestion", et(2026, 9, 4, 12), payload={"objective_id": "obj_a", "manual": True}, scope="objective:obj_a", by="owner:alice"),
        _row("ps_w40", "policy_suggestion", et(2026, 10, 4, 18), payload={"objective_id": "obj_a", "source": "weekly_outcomes"}, scope="objective:obj_a"),
        _row("pn", "playbook_note", et(2026, 9, 4, 12), scope="playbook"),
    ]
    plan = ex.plan_expiry(
        rows,
        now=now,
        hypothesis_status={"hyp_a": "active", "hyp_v": "validated", "hyp_w": "validated"},
        objective_status={"obj_a": "active", "obj_b": "active", "obj_arch": "archived"},
        pool_status={"c_exp": "expired", "c_open": "open"},
    )
    got = {e.id: e.reason for e in plan}
    assert got == {
        "v1": "superseded",
        "v_val": "hypothesis_inactive",
        "dd_gone": "hypothesis_missing",
        "cb_arch": "objective_inactive",
        "cb_closed": "batch_closed",
        "oi_due": "due",
        "dd_old": "due",  # legacy row, no expires_at: 10 sessions from created_at
        "ps_owner": "due",  # D3 keeps it from being covered; its 7-day clock still runs
    }
    by_id = {e.id: e for e in plan}
    assert by_id["v1"].superseded_by == "v2"
    assert by_id["cb_closed"].detail == {"pool": {"expired": 1, "missing": 1}}
    assert by_id["dd_old"].detail["expires_at_defaulted"] is True


def test_unreadable_subject_skips_the_rule() -> None:
    rows = [_row("dd", "decision_draft", MON_EOD, payload={"hypothesis_id": "hyp_x"}, scope="hypothesis:hyp_x")]
    assert ex.plan_expiry(rows, now=MON_EOD, hypothesis_status=None) == []


def _seed_sweep(db: FakeDB) -> None:
    db.hypotheses = {"hyp_a": "active", "hyp_v": "validated"}
    db.objectives = {"obj_a": "active", "obj_c": "active", "obj_arch": "archived"}
    db.pool = {"c1": "expired", "c2": "promoted", "c3": "open"}
    db.add("v_old", "eod_verdict", payload={"hypothesis_id": "hyp_a"}, scope="hyp_a", created_at=FRI - timedelta(days=3))
    db.add("v_new", "eod_verdict", payload={"hypothesis_id": "hyp_a"}, scope="hyp_a", created_at=FRI)
    db.add("v_val", "eod_verdict", payload={"hypothesis_id": "hyp_v"}, scope="hyp_v", created_at=FRI)
    db.add("cb_closed", "candidate_batch", payload={"objective_id": "obj_c", "items": [{"id": "c1"}, {"id": "c2"}]}, scope="objective:obj_c", created_at=FRI - timedelta(days=1))
    db.add("cb_live", "candidate_batch", payload={"objective_id": "obj_a", "items": [{"id": "c3"}]}, scope="objective:obj_a", created_at=FRI)
    db.add("cb_arch", "candidate_batch", payload={"objective_id": "obj_arch", "items": [{"id": "c3"}]}, scope="objective:obj_arch", created_at=FRI)
    db.add("pn", "playbook_note", scope="playbook", created_at=FRI - timedelta(days=30))
    db.add("done", "eod_verdict", payload={"hypothesis_id": "hyp_a"}, scope="hyp_a", created_at=FRI - timedelta(days=9), status="approved", action="executed")


def test_expire_due_sweeps_syncs_actions_and_leaves_the_pool() -> None:
    db = FakeDB(et(2026, 10, 5, 12))  # Monday midday: Friday's verdicts still live
    _seed_sweep(db)
    out = draft_repo.expire_due(db, now=db.clock, by="eod_agent")
    assert out["expired"] == 4
    assert out["by_reason"] == {"batch_closed": 1, "hypothesis_inactive": 1, "objective_inactive": 1, "superseded": 1}
    expired = {d for d, r in db.drafts.items() if r["status"] == "expired"}
    assert expired == {"v_old", "v_val", "cb_closed", "cb_arch"}
    assert db.drafts["v_old"]["payload"]["expired"] == {
        "reason": "superseded",
        "by": "eod_agent",
        "at": db.clock.isoformat(),
        "superseded_by": "v_new",
    }
    assert {a for a, r in db.actions.items() if r["status"] == "expired"} == {f"aal_{d}" for d in expired}
    assert db.actions["aal_done"]["status"] == "executed"
    assert db.drafts["done"]["status"] == "approved"
    # D8: the pool is read, never written — not even the still-open name of the archived batch.
    assert db.pool == {"c1": "expired", "c2": "promoted", "c3": "open"}
    assert db.pool_writes == []
    assert not any(s.startswith("update research.candidate_pool") for s in db.statements)


def test_expire_due_is_idempotent() -> None:
    db = FakeDB(et(2026, 10, 5, 12))
    _seed_sweep(db)
    draft_repo.expire_due(db, now=db.clock)
    db.statements.clear()
    again = draft_repo.expire_due(db, now=db.clock)
    assert again["expired"] == 0 and again["total"] == 0
    assert not any(s.startswith("update") for s in db.statements)


def test_expire_due_after_the_clock_runs_out() -> None:
    db = FakeDB(MON_EOD)  # after Monday's close: Friday's verdict is due
    _seed_sweep(db)
    draft_repo.expire_due(db, now=db.clock)
    assert db.drafts["v_new"]["status"] == "expired"
    assert db.drafts["v_new"]["payload"]["expired"]["reason"] == "due"
    assert db.drafts["cb_live"]["status"] == "pending"  # one name still open
    assert db.drafts["pn"]["status"] == "pending"  # D6


def test_dry_run_writes_nothing() -> None:
    db = FakeDB(MON_EOD)
    _seed_sweep(db)
    out = draft_repo.expire_due(db, now=db.clock, dry_run=True)
    assert out["total"] == 5 and out["expired"] == 0
    assert not any(s.startswith("update") for s in db.statements)
    assert db.commits == 0


# ─── readers ─────────────────────────────────────────────────────────────────


def test_pending_readers_filter_expires_at() -> None:
    db = FakeDB(MON_EOD)
    draft_repo.count_pending(db)
    draft_repo.count_pending(db, kind="eod_verdict")
    draft_repo.count_pending_by_kind(db)
    draft_repo.pending_inbox_cards(db, exclude_kinds={"eod_verdict"})
    draft_repo.list_drafts(db, status="pending")
    assert len(db.statements) == 5
    for s in db.statements:
        assert "expires_at is null or" in s and "expires_at > now()" in s, s
    db.statements.clear()
    draft_repo.list_drafts(db, status="approved")
    draft_repo.list_drafts(db, status=None)
    assert not any("expires_at >" in s for s in db.statements)


# ─── 409 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("draft", "reason"),
    [
        (
            {"id": "d1", "kind": "eod_verdict", "status": "expired", "payload": {"expired": {"reason": "superseded", "at": "t", "superseded_by": "d2"}}},
            "superseded",
        ),
        ({"id": "d1", "kind": "daily_digest", "status": "expired", "payload": {}}, "expired"),
        ({"id": "d1", "kind": "order_intent", "status": "pending", "payload": {}, "expires_at": "2026-10-01T00:00:00+00:00"}, "due"),
    ],
)
def test_expired_state(draft: dict[str, Any], reason: str) -> None:
    state = draft_repo.expired_state(draft, now=MON_EOD)
    assert state is not None
    assert state["code"] == "draft_expired"
    assert state["reason"] == reason


def test_live_drafts_have_no_expired_state() -> None:
    assert draft_repo.expired_state({"status": "pending", "expires_at": None}, now=MON_EOD) is None
    assert draft_repo.expired_state({"status": "pending", "expires_at": "2027-01-01T00:00:00+00:00"}, now=MON_EOD) is None
    assert draft_repo.expired_state({"status": "approved"}, now=MON_EOD) is None
