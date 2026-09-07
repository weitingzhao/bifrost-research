"""research-loop-automation D4 — what Copilot and the Loop said about one symbol."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bifrost_research.copilot.agents import symbol_verdicts as SV

DIGEST = {
    "id": "drf_digest",
    "kind": "daily_digest",
    "status": "pending",
    "payload": {
        "day": "2026-09-07",
        "exhibits": {"NVDA": [{"lens": "iv_rank", "band": "cold", "value": 18.7, "means": "premium cheap", "as_of": "2026-09-04"}, {"lens": "vrp", "band": None, "value": None, "means": None, "as_of": None}]},
        "candidates": [{"symbol": "NVDA"}, {"symbol": "WT"}],
        "batches": [{"run_id": "run_1", "objective_title": "Daily Loop Stock Explorer", "status": "awaiting_approval", "candidates": ["NVDA", "WT"], "auto_accepted": ["WT"], "held": [{"symbol": "NVDA", "reasons": ["judges did not agree (dissent)", "net stance dissent"]}]}],
        "dissents": [{"symbol": "NVDA", "run_id": "run_1", "objective_title": "Daily Loop Stock Explorer", "net_stance": "dissent", "blocked_by_validate": False, "judges": ["deepseek-chat: support", "gpt-4o-mini: abstain"], "wrong_if": ["path leaves PIVOT"]}],
        "resolutions": [{"id": "hyp_lpg", "title": "LPG breakout", "status": "validated", "symbols": ["LPG"], "decision": "validated", "excess": 0.03, "by_rule": True}],
    },
}


def test_digest_view_reads_the_symbol_lines() -> None:
    v = SV.digest_view(DIGEST, "NVDA")
    assert v is not None and v["day"] == "2026-09-07" and v["draft_id"] == "drf_digest"
    assert [ln["band"] for ln in v["lenses"]] == ["cold", None]
    assert v["proposed"] is True
    assert v["batches"] == [{"run_id": "run_1", "objective_title": "Daily Loop Stock Explorer", "status": "awaiting_approval", "auto_accepted": False, "held_reasons": ["judges did not agree (dissent)", "net stance dissent"]}]
    assert v["dissent"]["judges"] == ["deepseek-chat: support", "gpt-4o-mini: abstain"] and v["resolution"] is None
    assert v["line"] == "iv_rank cold · vrp no reading · held: judges did not agree (dissent) · judges split"

    wt = SV.digest_view(DIGEST, "WT")
    assert wt is not None and wt["lenses"] == [] and wt["batches"][0]["auto_accepted"] is True and wt["line"] == "auto-accepted by the leash"
    lpg = SV.digest_view(DIGEST, "LPG")
    assert lpg is not None and lpg["resolution"]["status"] == "validated" and lpg["line"] == "hypothesis validated"
    assert SV.digest_view(DIGEST, "AAPL") is None
    assert SV.digest_view(None, "NVDA") is None


def test_proposals_carry_their_approval_state_newest_first(monkeypatch) -> None:
    monkeypatch.setattr(SV.draft_repo, "list_drafts", lambda conn, **kw: _drafts(kw))
    monkeypatch.setattr(SV.cand_repo, "list_candidates", lambda conn, **kw: [{"id": "cand_1", "symbol": "NVDA", "status": "open", "source": "copilot", "score": 70.0, "hypothesis_id": None, "created_at": "2026-09-07T03:00:00+00:00"}] if kw.get("symbol") == "NVDA" else [])
    monkeypatch.setattr(SV.hyp_repo, "list_hypotheses", lambda conn, **kw: [{"id": "hyp_1", "title": "NVDA IV extreme short", "status": "active", "origin_page": "copilot", "created_at": "2026-09-06T20:00:00+00:00", "updated_at": "2026-09-06T20:00:00+00:00", "resolution_json": None}])
    monkeypatch.setattr(
        SV.action_repo,
        "list_actions",
        lambda conn, **kw: [
            {"id": "act_1", "action_kind": "research.loop.propose_candidate", "status": "executed", "approved_by": "owner", "session_id": "s1", "created_at": "2026-09-07T03:00:01+00:00", "input": {"tool_name": "research.loop.propose_candidate", "arguments": {"symbol": "nvda", "source": "copilot"}}},
            {"id": "act_2", "action_kind": "research.hypothesis.create", "status": "proposed", "created_at": "2026-09-07T02:00:00+00:00", "input": {"arguments": {"symbols": ["AAPL"]}}},
        ],
    )

    out = SV.build_symbol_verdicts(object(), "nvda", now=datetime(2026, 9, 7, 5, tzinfo=timezone.utc))
    assert out["symbol"] == "NVDA" and out["digest"]["line"].startswith("iv_rank cold")
    kinds = [(p["kind"], p["state"]) for p in out["proposals"]]
    assert kinds == [("action", "executed"), ("candidate", "proposed"), ("draft", "awaiting approval"), ("hypothesis", "active")]
    assert out["proposals"][0]["tool"] == "research.loop.propose_candidate" and out["proposals"][0]["title"] == "propose candidate"
    assert out["proposals"][1]["by_copilot"] is True and out["proposals"][3]["by_copilot"] is True
    assert out["proposals"][2]["draft_kind"] == "order_intent" and out["proposals"][2]["title"] == "NVDA short strangle"
    assert out["counts"] == {"action": 1, "candidate": 1, "draft": 1, "hypothesis": 1}
    assert out["advisory"].startswith("D10 BLOCKED")


def _drafts(kw: dict[str, Any]) -> list[dict[str, Any]]:
    if kw.get("kind") == "daily_digest":
        return [DIGEST] if kw.get("status") == "pending" else []
    if kw.get("kind") == "order_intent":
        return [
            {"id": "drf_oi", "kind": "order_intent", "status": "pending", "generated_by": "copilot", "created_at": "2026-09-07T01:00:00+00:00", "payload": {"symbol": "NVDA", "title": "NVDA short strangle"}},
            {"id": "drf_other", "kind": "order_intent", "status": "pending", "payload": {"symbols": ["AAPL"]}},
        ]
    return []


def test_nothing_said_is_an_empty_answer_not_an_error(monkeypatch) -> None:
    monkeypatch.setattr(SV.draft_repo, "list_drafts", lambda conn, **kw: [])
    monkeypatch.setattr(SV.cand_repo, "list_candidates", lambda conn, **kw: [])
    monkeypatch.setattr(SV.hyp_repo, "list_hypotheses", lambda conn, **kw: [])
    monkeypatch.setattr(SV.action_repo, "list_actions", lambda conn, **kw: [])
    out = SV.build_symbol_verdicts(object(), "AAPL")
    assert out["digest"] is None and out["proposals"] == [] and out["counts"] == {}
