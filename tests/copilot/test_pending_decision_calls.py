"""The Inbox badge and the Inbox page must count the same queue.

The badge reads ``pending_decisions.calls``. It used to be counted off the
newest 500 pending drafts of every kind; with 725 briefings pending on DEV
(2026-10-04), 443 of those 500 were briefings, and the badge said 57 while the
Decision Inbox — which reads every kind in full — drew 58 cards and 725
briefings. The count is now taken in SQL over the whole pending set, folded
the way the page folds its cards (``inboxCards.ts``, design Rev .143):

- a hypothesis's ``decision_draft`` and ``order_intent`` are one call, and a
  blank ``hypothesis_id`` never merges;
- an objective's ``candidate_batch`` runs are one card, as are its
  objective-scoped ``policy_suggestion`` patches — the newest covers the rest;
- everything else is one card per draft.

The repository returns one row per card. ``cards_of`` below plays the part of
that SQL for the aggregation tests; the SQL itself is pinned by the
repository tests at the end of this file.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pytest

from bifrost_research.copilot.harness import standing
from bifrost_research.repositories import ai_draft as draft_repo

_seq = 0


def draft(kind: str, payload: dict[str, Any] | None = None, *, scope: str = "global") -> dict[str, Any]:
    global _seq
    _seq += 1
    return {"id": f"drf_{_seq:06d}", "kind": kind, "payload": payload or {}, "scope": scope, "created_at": _seq}


def batch(objective: str, *symbols: str) -> dict[str, Any]:
    return draft(
        "candidate_batch",
        {"objective_id": objective, "items": [{"symbol": s} for s in symbols]},
        scope=f"objective:{objective}",
    )


def call(kind: str, hypothesis: Any) -> dict[str, Any]:
    return draft(kind, {"hypothesis_id": hypothesis}, scope=f"hypothesis:{hypothesis}")


def patch(objective: str, suggestion: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    return draft(
        "policy_suggestion",
        {"objective_id": objective, "suggestion": suggestion, "current_policy": current},
        scope=f"objective:{objective}",
    )


def _text(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _card_key(d: dict[str, Any]) -> str:
    p, kind, scope = d["payload"], d["kind"], d["scope"]
    obj = _text(p.get("objective_id")) or (_text(scope[len("objective:") :]) if scope.startswith("objective:") else None)
    if kind in ("decision_draft", "order_intent") and _text(p.get("hypothesis_id")):
        return f"call:{_text(p.get('hypothesis_id'))}"
    if kind == "candidate_batch" and obj:
        return f"pool:{obj}"
    if kind == "policy_suggestion" and scope.startswith("objective:") and obj:
        return f"patch:{obj}"
    return f"draft:{d['id']}"


def cards_of(rows: list[dict[str, Any]], exclude_kinds: frozenset[str]) -> list[dict[str, Any]]:
    """What ``pending_inbox_cards`` returns for these rows: one row per card, newest head."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for d in rows:
        if d["kind"] in exclude_kinds:
            continue
        groups.setdefault(_card_key(d), []).append(d)
    out = []
    for key, members in groups.items():
        head = max(members, key=lambda d: (d["created_at"], d["id"]))
        out.append(
            {
                "card_key": key,
                "n": len(members),
                "verdicts": sum(1 for d in members if d["kind"] == "decision_draft"),
                "vehicles": sum(1 for d in members if d["kind"] == "order_intent"),
                "kind": head["kind"],
                "scope": head["scope"],
                "payload": head["payload"] if head["kind"] == "policy_suggestion" else None,
            }
        )
    return out


def count(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> dict[str, Any]:
    def no_page(*a: Any, **k: Any) -> Any:
        raise AssertionError("the badge count must not read a page of drafts")

    monkeypatch.setattr(draft_repo, "list_drafts", no_page)
    monkeypatch.setattr(draft_repo, "count_pending_by_kind", lambda conn: dict(Counter(d["kind"] for d in rows)))
    monkeypatch.setattr(
        draft_repo,
        "pending_inbox_cards",
        lambda conn, *, exclude_kinds: cards_of(rows, frozenset(exclude_kinds)),
    )
    return standing.pending_decision_calls(object())


# ── the whole queue, not a page of it ─────────────────────────────────────


def test_counts_past_the_old_500_row_page(monkeypatch: pytest.MonkeyPatch) -> None:
    # DEV's shape, scaled past the old ceiling: briefings first in time order
    # would have filled the newest-500 page and pushed the calls out of it.
    rows: list[dict[str, Any]] = []
    rows += [draft("eod_verdict") for _ in range(1200)]
    rows += [draft("daily_digest")]
    for i in range(60):
        rows += [call("decision_draft", f"hyp-{i}"), call("order_intent", f"hyp-{i}")]
    rows += [call("decision_draft", "") for _ in range(4)]
    rows += [batch("obj-a", f"S{i}") for i in range(39)]
    rows += [patch("obj-a", {"max_candidates": 10}, {"max_candidates": 8}) for _ in range(29)]
    rows += [draft("playbook_note") for _ in range(7)]
    rows += [draft("eod_verdict") for _ in range(300)]
    assert len(rows) > 1500

    out = count(monkeypatch, rows)
    # 60 calls + 4 unmerged blank-hypothesis drafts + 1 pool + 1 patch + 7 notes.
    assert out["calls"] == 73
    assert out["briefings"] == 1501
    assert out["pending"] == len(rows)
    assert out["by_kind"]["eod_verdict"] == 1500
    assert out["drafts"] == 120 + 4 + 39 + 29 + 7
    assert out["folded"] == 38 + 28
    # Blank-hypothesis decision drafts are single cards whose Approve writes nothing.
    assert out["inert"] == 4


def test_the_count_agrees_with_the_inbox_on_devs_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    # DEV 2026-10-04 by kind: decision_draft 53 · order_intent 12 ·
    # policy_suggestion 33 · candidate_batch 39 · playbook_note 7 ·
    # eod_verdict 724 · daily_digest 1 = 869, which the Inbox draws as 58 cards.
    rows: list[dict[str, Any]] = []
    rows += [draft("eod_verdict") for _ in range(724)] + [draft("daily_digest")]
    for i in range(12):  # twelve hypotheses carry both a verdict and a vehicle
        rows += [call("decision_draft", f"both-{i}"), call("order_intent", f"both-{i}")]
    for i in range(6):  # six carry two verdicts
        rows += [call("decision_draft", f"twice-{i}"), call("decision_draft", f"twice-{i}")]
    rows += [call("decision_draft", f"once-{i}") for i in range(25)]
    rows += [call("decision_draft", "") for _ in range(4)]
    rows += [batch("obj-a", "NVDA") for _ in range(20)] + [batch("obj-b", "HALO") for _ in range(19)]
    rows += [patch("obj-a", {"max_candidates": 10}, {"max_candidates": 8}) for _ in range(29)]
    rows += [patch("obj-b", {"max_candidates": 8}, {"max_candidates": 8}) for _ in range(4)]
    rows += [draft("playbook_note") for _ in range(7)]
    assert Counter(d["kind"] for d in rows) == {
        "eod_verdict": 724,
        "decision_draft": 53,
        "candidate_batch": 39,
        "policy_suggestion": 33,
        "order_intent": 12,
        "playbook_note": 7,
        "daily_digest": 1,
    }

    out = count(monkeypatch, rows)
    assert out["calls"] == 12 + 6 + 25 + 4 + 2 + 2 + 7 == 58
    assert out["briefings"] == 725
    assert out["pending"] == 869


# ── what counts as a call ────────────────────────────────────────────────


def test_a_briefing_is_not_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    out = count(monkeypatch, [draft("morning_brief"), draft("eod_verdict"), draft("daily_digest")])
    assert out["calls"] == 0
    assert out["briefings"] == 3


def test_an_objectives_runs_are_one_card_whatever_names_they_propose(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [batch("obj-a", "NVDA", "HALO") for _ in range(6)] + [batch("obj-a", "AMD") for _ in range(5)]
    out = count(monkeypatch, rows)
    assert out["calls"] == 1
    assert out["folded"] == 10
    assert out["drafts"] == 11


def test_another_objective_is_its_own_card(monkeypatch: pytest.MonkeyPatch) -> None:
    out = count(monkeypatch, [batch("obj-a", "NVDA"), batch("obj-b", "NVDA")])
    assert out["calls"] == 2


def test_a_batch_with_no_objective_stands_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [draft("candidate_batch", {"items": [{"symbol": "NVDA"}]}) for _ in range(2)]
    out = count(monkeypatch, rows)
    assert out["calls"] == 2 and out["folded"] == 0


def test_a_verdict_and_its_vehicle_are_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    out = count(monkeypatch, [call("decision_draft", "h1"), call("order_intent", "h1")])
    assert out["calls"] == 1 and out["folded"] == 0 and out["drafts"] == 2


def test_an_older_verdict_folds_under_the_newest(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [call("decision_draft", "h1"), call("decision_draft", "h1"), call("order_intent", "h1")]
    out = count(monkeypatch, rows)
    assert out["calls"] == 1 and out["folded"] == 1


def test_a_blank_hypothesis_never_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [call("decision_draft", ""), call("decision_draft", "  "), call("order_intent", None)]
    out = count(monkeypatch, rows)
    assert out["calls"] == 3


def test_objective_patches_fold_global_suggestions_do_not(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [patch("obj-a", {"max_candidates": 10}, {"max_candidates": 8}) for _ in range(3)]
    rows += [draft("policy_suggestion", {"suggestion": {"max_candidates": 10}, "current_policy": {}}) for _ in range(2)]
    out = count(monkeypatch, rows)
    assert out["calls"] == 1 + 2


def test_a_patch_that_would_write_nothing_is_a_card_named_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Inbox draws it (and says it writes nothing), so the badge counts it.
    out = count(monkeypatch, [patch("obj-a", {"max_candidates": 8}, {"max_candidates": 8})])
    assert out["calls"] == 1 and out["inert"] == 1


def test_inert_reads_the_newest_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        patch("obj-a", {"max_candidates": 8}, {"max_candidates": 8}),
        patch("obj-a", {"max_candidates": 10}, {"max_candidates": 8}),
    ]
    out = count(monkeypatch, rows)
    assert out["calls"] == 1 and out["inert"] == 0


def test_a_playbook_note_is_a_card_that_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    out = count(monkeypatch, [draft("playbook_note"), draft("playbook_note")])
    assert out["calls"] == 2 and out["inert"] == 0


def test_an_unmodelled_kind_still_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    # By exclusion on purpose: an allowlist would hide exactly the draft a
    # human most needs to see.
    out = count(monkeypatch, [draft("something_new")])
    assert out["calls"] == 1 and out["inert"] == 1


def test_a_lookup_failure_reports_zero_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(draft_repo, "count_pending_by_kind", boom)
    out = standing.pending_decision_calls(object())
    assert out["calls"] == 0 and out["briefings"] == 0


# ── the whitelist follows the author ─────────────────────────────────────


def test_an_owner_may_move_a_knob_a_model_may_not() -> None:
    payload = {"manual": True, "suggestion": {"decline_memory": {"enabled": False}}, "current_policy": {}}
    assert standing.policy_suggestion_writes(payload) == 1


def test_the_same_key_from_a_model_writes_nothing() -> None:
    # Approval would drop it, so the Inbox must not offer it as a call.
    payload = {"suggestion": {"decline_memory": {"enabled": False}}, "current_policy": {}}
    assert standing.policy_suggestion_writes(payload) == 0


def test_either_marker_the_endpoint_stamps_means_owner() -> None:
    for marker in ({"manual": True}, {"source": "owner"}):
        payload = {**marker, "suggestion": {"use_llm_plan": True}, "current_policy": {}}
        assert standing.policy_suggestion_writes(payload) == 1


def test_a_key_on_neither_whitelist_never_counts() -> None:
    payload = {"manual": True, "suggestion": {"not_a_policy_key": 1}, "current_policy": {}}
    assert standing.policy_suggestion_writes(payload) == 0


# ── the SQL: grouped on the server, no page size ──────────────────────────


class _Cursor:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.sql = ""
        self.params: Any = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql, self.params = sql, params

    def fetchall(self) -> list[Any]:
        return self.rows


class _Conn:
    def __init__(self, rows: list[Any]) -> None:
        self.cur = _Cursor(rows)

    def cursor(self) -> _Cursor:
        return self.cur

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def _squash(sql: str) -> str:
    return " ".join(sql.split())


def test_count_by_kind_groups_in_sql_without_a_limit() -> None:
    conn = _Conn([("eod_verdict", 724), {"kind": "decision_draft", "count": 53}])
    assert draft_repo.count_pending_by_kind(conn) == {"eod_verdict": 724, "decision_draft": 53}
    sql = _squash(conn.cur.sql)
    assert "GROUP BY kind" in sql and "status = 'pending'" in sql
    assert "LIMIT" not in sql.upper()


def test_inbox_cards_fold_in_sql_without_a_limit() -> None:
    conn = _Conn(
        [
            ("call:h1", 2, 1, 1, "decision_draft", "hypothesis:h1", None),
            ("patch:obj-a", 29, 0, 0, "policy_suggestion", "objective:obj-a", json.dumps({"suggestion": {}})),
        ]
    )
    rows = draft_repo.pending_inbox_cards(conn, exclude_kinds=standing.BRIEFING_KINDS)
    assert rows[0] == {
        "card_key": "call:h1",
        "n": 2,
        "verdicts": 1,
        "vehicles": 1,
        "kind": "decision_draft",
        "scope": "hypothesis:h1",
        "payload": None,
    }
    assert rows[1]["payload"] == {"suggestion": {}}
    sql = _squash(conn.cur.sql)
    assert "LIMIT" not in sql.upper()
    assert "status = 'pending'" in sql and "NOT (a.kind = ANY(%s))" in sql
    assert conn.cur.params == (sorted(standing.BRIEFING_KINDS),)
    # The page's card keys, and its tie-break for the head (newest, then id).
    for key in ("'call:' || k.hyp", "'pool:' || k.obj", "'patch:' || k.obj", "'draft:' || a.id"):
        assert key in sql
    assert "ORDER BY card_key, created_at DESC, id DESC" in sql
    # psycopg2 formats this string: a literal % must be doubled.
    assert "LIKE 'objective:%%'" in sql
