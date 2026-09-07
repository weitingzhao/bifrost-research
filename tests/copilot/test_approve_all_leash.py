"""research-loop-automation D3 — approve-all runs every candidate through the leash."""

from __future__ import annotations

from typing import Any

import pytest

from bifrost_research.copilot.harness import batch as B


class _Conn:
    def rollback(self) -> None:
        pass


def _item(cid: str, symbol: str, *, agreement: str = "agree", net: str = "support", hit_rate: float | None = 0.6) -> dict[str, Any]:
    track = {"status": "ok", "horizons": [{"horizon_days": 5, "judged": 9, "hit_rate": hit_rate}]} if hit_rate is not None else {"status": "not_measured"}
    return {
        "id": cid,
        "symbol": symbol,
        "agreement": agreement,
        "net_stance": net,
        "blocked_by_validate": False,
        "evidence": {"selection": {"status": "ok"}, "track_record": track},
    }


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake repositories around one run with one pending candidate_batch draft."""
    from bifrost_research.api import agents as api_agents
    from bifrost_research.copilot.curator import runtime as curator_rt
    from bifrost_research.copilot.harness import validate_hook

    w: dict[str, Any] = {"draft": None, "promoted": [], "approved": [], "patched": [], "actions": [], "status": []}
    monkeypatch.setattr(B.obj_repo, "get_run", lambda conn, rid: {"id": rid, "outputs": {"draft_ids": ["d1"]}})
    monkeypatch.setattr(B.obj_repo, "update_run_status", lambda conn, rid, *, status: w["status"].append(status))
    monkeypatch.setattr(B.draft_repo, "get_draft", lambda conn, did: w["draft"] if did == "d1" else None)
    monkeypatch.setattr(B.draft_repo, "patch_draft_payload", lambda conn, did, payload: w["patched"].append((did, payload)) or {})

    def _promote(conn, *, draft_id, payload):
        w["promoted"].append([i["symbol"] for i in payload["items"]])
        return {"promoted": payload["items"], "hypotheses": [{"id": f"hyp_{i['symbol']}"} for i in payload["items"]], "skipped": []}

    monkeypatch.setattr(api_agents, "_promote_candidate_batch", _promote)

    def _approve(conn, draft, *, approved_by, owner_id):
        w["approved"].append(draft)
        return {"executed": {"hypotheses": [{"id": f"hyp_{i['symbol']}"} for i in draft["payload"]["items"]]}}

    monkeypatch.setattr(api_agents, "apply_draft_approval", _approve)
    monkeypatch.setattr(validate_hook, "run_validate_hooks_for_run", lambda conn, **kw: {"validated": kw.get("hypothesis_ids")})
    monkeypatch.setattr(curator_rt, "run_curator_for_run", lambda conn, rid, **kw: {"status": "ok"})
    from bifrost_research.repositories import ai_action_log as action_repo

    monkeypatch.setattr(action_repo, "insert_action", lambda conn, **kw: w["actions"].append(kw) or {"id": "act"})
    return w


def test_partial_batch_promotes_the_passing_names_and_keeps_the_draft_pending(world) -> None:
    world["draft"] = {"id": "d1", "kind": "candidate_batch", "status": "pending", "payload": {"items": [_item("c_wt", "WT"), _item("c_rklb", "RKLB", agreement="dissent", net="dissent")]}}

    out = B.approve_all_for_run(_Conn(), "run_1", approved_by="system:loop_batch", kinds_whitelist=B.RESEARCH_AUTO_APPROVE_KINDS, auto_validate=True)

    assert world["promoted"] == [["WT"]] and world["approved"] == []
    assert out["partial"] == ["d1"] and out["approved"] == [] and out["count"] == 0
    assert out["accepted_symbols"] == ["WT"] and out["accepted_count"] == 1
    assert out["held_symbols"] == [{"draft_id": "d1", "id": "c_rklb", "symbol": "RKLB", "reasons": ["judges did not agree (dissent)", "net stance dissent"]}]
    assert out["held"] == [{"draft_id": "d1", "reason": "leash_held_some", "held": 1}]
    assert out["hypothesis_ids"] == ["hyp_WT"] and out["validate"] == {"validated": ["hyp_WT"]}
    assert world["status"] == []  # still awaiting the Owner for RKLB
    did, payload = world["patched"][0]
    assert did == "d1" and payload["leash"]["accepted"] == [{"id": "c_wt", "symbol": "WT"}]
    assert payload["leash"]["held"][0]["symbol"] == "RKLB" and payload["leash"]["min_source_hit_rate"] == 0.45
    assert world["actions"][0]["action_kind"] == "loop_auto_accept" and world["actions"][0]["output_payload"]["accepted"] == ["WT"]


def test_a_batch_that_passes_whole_is_approved_with_the_leash_on_it(world) -> None:
    world["draft"] = {"id": "d1", "kind": "candidate_batch", "status": "pending", "payload": {"items": [_item("c_wt", "WT"), _item("c_lpg", "LPG")]}}

    out = B.approve_all_for_run(_Conn(), "run_1", kinds_whitelist=B.RESEARCH_AUTO_APPROVE_KINDS, min_source_hit_rate=0.5)

    assert world["promoted"] == [] and len(world["approved"]) == 1
    assert world["approved"][0]["payload"]["leash"]["accepted"] == [{"id": "c_wt", "symbol": "WT"}, {"id": "c_lpg", "symbol": "LPG"}]
    assert out["approved"] == ["d1"] and out["count"] == 1 and out["held"] == [] and out["accepted_symbols"] == ["WT", "LPG"]
    assert out["leash"] == {"min_source_hit_rate": 0.5}
    assert world["status"] == ["completed"]


def test_a_batch_the_leash_holds_entirely_writes_nothing_but_the_reasons(world) -> None:
    world["draft"] = {"id": "d1", "kind": "candidate_batch", "status": "pending", "payload": {"items": [_item("c_wt", "WT", hit_rate=None), _item("c_lpg", "LPG", hit_rate=0.3)]}}

    out = B.approve_all_for_run(_Conn(), "run_1", kinds_whitelist=B.RESEARCH_AUTO_APPROVE_KINDS)

    assert world["promoted"] == [] and world["approved"] == [] and world["status"] == []
    assert out["held"] == [{"draft_id": "d1", "reason": "leash_held_all", "held": 2}] and out["accepted_symbols"] == []
    assert [h["reasons"] for h in out["held_symbols"]] == [["source track record not measured"], ["source hit rate 30% < 45% at T+5"]]
    assert world["patched"][0][1]["leash"]["held"][1]["symbol"] == "LPG"
    assert world["actions"][0]["output_payload"]["held"][0]["symbol"] == "WT"


def test_other_whitelisted_kinds_still_approve_and_policy_suggestions_never_do(world) -> None:
    world["draft"] = {"id": "d1", "kind": "policy_suggestion", "status": "pending", "payload": {"suggestion": {"max_candidates": 2}}}
    out = B.approve_all_for_run(_Conn(), "run_1", kinds_whitelist=B.RESEARCH_AUTO_APPROVE_KINDS)
    assert world["approved"] == [] and out["count"] == 0

    world["draft"] = {"id": "d1", "kind": "eod_verdict", "status": "pending", "payload": {"items": []}}
    out = B.approve_all_for_run(_Conn(), "run_1", kinds_whitelist=B.RESEARCH_AUTO_APPROVE_KINDS)
    assert len(world["approved"]) == 1 and out["approved"] == ["d1"]
