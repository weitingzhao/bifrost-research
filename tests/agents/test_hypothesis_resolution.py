"""B3 (research-loop-automation) — hypotheses resolve themselves by outcome rule."""

from __future__ import annotations

from typing import Any

import pytest

from bifrost_research.copilot.agents import eod_review
from bifrost_research.copilot.agents import hypothesis_resolution as hr
from bifrost_research.copilot.harness.policy_schema import ResolutionPolicy, parse_policy


def _hyp(hid: str, *, candidate_id: str | None = "cand_1", objective_id: str | None = "obj-x") -> dict[str, Any]:
    ref: dict[str, Any] = {}
    if candidate_id:
        ref["candidate_id"] = candidate_id
    if objective_id:
        ref["objective_id"] = objective_id
    return {"id": hid, "title": f"Thesis {hid}", "symbols": ["NVDA"], "status": "active", "origin_ref": ref or None}


def _outcome(excess: float | None, horizon: int = 20) -> dict[str, Any]:
    return {
        "candidate_id": "cand_1",
        "symbol": "NVDA",
        "trade_date": "2026-08-03",
        "horizon_days": horizon,
        "exit_date": "2026-08-31",
        "entry_close": 100.0,
        "exit_close": 106.0,
        "forward_return": 0.06,
        "benchmark_symbol": "SPY",
        "benchmark_return": None if excess is None else 0.06 - excess,
        "excess_return": excess,
        "hit": None if excess is None else excess > 0,
        "settled_at": "2026-08-31T21:00:00+00:00",
    }


class _Conn:
    def rollback(self) -> None:
        return None


@pytest.fixture
def repos(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake repositories: what the resolver read and what it wrote."""
    state: dict[str, Any] = {
        "active": [],
        "outcomes": {},
        "policies": {},
        "resolved": [],
        "actions": [],
        "drafts": [],
    }
    monkeypatch.setattr(hr.hyp_repo, "list_hypotheses", lambda conn, **k: list(state["active"]))
    monkeypatch.setattr(
        hr, "load_outcome", lambda conn, cid, horizon: state["outcomes"].get((cid, horizon))
    )
    monkeypatch.setattr(
        hr.obj_repo, "get_objective", lambda conn, oid: {"id": oid, "policy_json": state["policies"].get(oid, {})}
    )
    monkeypatch.setattr(hr.obj_repo, "get_run", lambda conn, rid: {"id": rid, "objective_id": "obj-from-run"})

    def _resolve(conn, hid, *, status, conclusion, resolution):
        state["resolved"].append((hid, status, conclusion, resolution))
        return {"id": hid, "status": status}

    monkeypatch.setattr(hr.hyp_repo, "resolve_hypothesis", _resolve)

    def _action(conn, **kw):
        state["actions"].append(kw)
        return {"id": f"aal_{len(state['actions'])}"}

    monkeypatch.setattr(hr.action_repo, "insert_action", _action)

    def _draft(conn, **kw):
        state["drafts"].append(kw)
        return {"id": f"drf_{len(state['drafts'])}"}

    monkeypatch.setattr(hr.draft_repo, "insert_draft", _draft)
    return state


# --------------------------------------------------------------------------- #
# the rule
# --------------------------------------------------------------------------- #


def test_decide_matrix() -> None:
    rule = ResolutionPolicy()
    assert hr.decide(0.03, rule)[0] == hr.VALIDATED
    assert hr.decide(0.0299, rule)[0] == hr.AMBIGUOUS
    assert hr.decide(-0.03, rule)[0] == hr.REJECTED
    assert hr.decide(-0.0299, rule)[0] == hr.AMBIGUOUS
    assert hr.decide(None, rule) == (hr.PENDING, "horizon 20 sessions not settled yet")
    tight = ResolutionPolicy(horizon_days=5, validate_excess=0.01, reject_excess=-0.01)
    decision, reason = hr.decide(0.012, tight)
    assert decision == hr.VALIDATED and "5 sessions" in reason and "+1.20%" in reason


def test_policy_carries_the_rule_with_defaults() -> None:
    policy = parse_policy({"universe_mode": "stock_composite"})
    assert policy.resolution.model_dump() == {
        "enabled": True,
        "horizon_days": 20,
        "validate_excess": 0.03,
        "reject_excess": -0.03,
        "benchmark": "SPY",
    }
    custom = parse_policy({"resolution": {"horizon_days": 5, "validate_excess": 0.02}})
    assert (custom.resolution.horizon_days, custom.resolution.validate_excess) == (5, 0.02)


# --------------------------------------------------------------------------- #
# resolve_active
# --------------------------------------------------------------------------- #


def test_clear_outcome_is_applied_with_a_receipt(repos: dict[str, Any]) -> None:
    repos["active"] = [_hyp("h-win"), _hyp("h-loss", candidate_id="cand_2")]
    repos["outcomes"][("cand_1", 20)] = _outcome(0.042)
    repos["outcomes"][("cand_2", 20)] = {**_outcome(-0.051), "candidate_id": "cand_2"}

    summary = hr.resolve_active(_Conn())

    assert summary["counts"] == {"validated": 1, "rejected": 1, "ambiguous": 0, "pending": 0, "no_lineage": 0}
    assert summary["applied"] == ["h-win", "h-loss"]
    statuses = {hid: status for hid, status, _, _ in repos["resolved"]}
    assert statuses == {"h-win": "validated", "h-loss": "rejected"}
    _, _, conclusion, record = repos["resolved"][0]
    assert "held up by outcome rule" in conclusion and "+4.20%" in conclusion
    assert record["decision"] == "validated"
    assert record["outcome"]["excess_return"] == 0.042
    assert record["rule"]["horizon_days"] == 20
    assert record["lineage"]["candidate_id"] == "cand_1"
    # The ledger row and the informational briefing.
    assert [a["action_kind"] for a in repos["actions"]] == [hr.ACTION_KIND] * 2
    assert repos["actions"][0]["status"] == "executed"
    assert [d["kind"] for d in repos["drafts"]] == ["eod_verdict", "eod_verdict"]
    assert repos["drafts"][0]["payload"]["auto_resolved"] is True
    assert repos["drafts"][0]["payload"]["applied"] is True
    assert repos["drafts"][0]["payload"]["proposed_status"] == "validated"
    assert repos["drafts"][0]["linked_action_id"] == "aal_1"


def test_ambiguous_pending_and_no_lineage_write_nothing(repos: dict[str, Any]) -> None:
    repos["active"] = [
        _hyp("h-meh"),
        _hyp("h-wait", candidate_id="cand_9"),
        _hyp("h-manual", candidate_id=None, objective_id=None),
    ]
    repos["outcomes"][("cand_1", 20)] = _outcome(0.012)

    summary = hr.resolve_active(_Conn())

    by_id = {e["id"]: e for e in summary["entries"]}
    assert by_id["h-meh"]["decision"] == hr.AMBIGUOUS and "dead band" in by_id["h-meh"]["reason"]
    assert by_id["h-wait"]["decision"] == hr.PENDING
    assert by_id["h-manual"]["decision"] == hr.NO_LINEAGE
    assert summary["applied"] == []
    assert repos["resolved"] == [] and repos["actions"] == [] and repos["drafts"] == []


def test_rule_comes_from_the_objective_and_dry_run_previews(repos: dict[str, Any]) -> None:
    repos["policies"]["obj-x"] = {"resolution": {"horizon_days": 5, "validate_excess": 0.01, "reject_excess": -0.01}}
    repos["active"] = [_hyp("h-5")]
    repos["outcomes"][("cand_1", 5)] = _outcome(0.015, horizon=5)
    repos["outcomes"][("cand_1", 20)] = None

    preview = hr.resolve_active(_Conn(), dry_run=True)
    assert preview["entries"][0]["decision"] == hr.VALIDATED
    assert preview["entries"][0]["horizon_days"] == 5
    assert preview["dry_run"] is True and repos["resolved"] == []

    # A horizon override reads a different window without touching the policy.
    override = hr.resolve_active(_Conn(), dry_run=True, horizon_override=20)
    assert override["entries"][0]["decision"] == hr.PENDING


def test_rule_falls_back_through_the_run_and_to_defaults(repos: dict[str, Any]) -> None:
    repos["policies"]["obj-from-run"] = {"resolution": {"enabled": False}}
    via_run = {"id": "h-run", "title": "t", "symbols": ["EE"], "status": "active", "origin_ref": {"candidate_id": "cand_1", "run_id": "run_1"}}
    repos["active"] = [via_run]
    summary = hr.resolve_active(_Conn())
    assert summary["entries"][0] == {"id": "h-run", "decision": hr.NO_LINEAGE, "reason": "resolution rule disabled"}

    cache: dict[str, ResolutionPolicy] = {}
    assert hr.rule_for(_Conn(), _hyp("h", objective_id=None), cache) == ResolutionPolicy()


def test_a_failed_write_is_recorded_not_raised(repos: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    repos["active"] = [_hyp("h-boom")]
    repos["outcomes"][("cand_1", 20)] = _outcome(0.05)

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(hr.hyp_repo, "resolve_hypothesis", _boom)
    summary = hr.resolve_active(_Conn())
    entry = summary["entries"][0]
    assert entry["decision"] == hr.VALIDATED and entry["applied"] is False and "db down" in entry["error"]
    assert summary["applied"] == []


# --------------------------------------------------------------------------- #
# the EOD review reads the rule first
# --------------------------------------------------------------------------- #


def test_eod_review_resolves_first_and_drafts_the_rest(repos: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    resolved_ids: set[str] = set()

    def _resolve(conn, hid, *, status, conclusion, resolution):
        resolved_ids.add(hid)
        return {"id": hid, "status": status}

    monkeypatch.setattr(hr.hyp_repo, "resolve_hypothesis", _resolve)
    all_active = [_hyp("h-win"), _hyp("h-meh", candidate_id="cand_2"), _hyp("h-manual", candidate_id=None)]
    repos["outcomes"][("cand_1", 20)] = _outcome(0.05)
    repos["outcomes"][("cand_2", 20)] = {**_outcome(0.01), "candidate_id": "cand_2"}
    # Second listing (after resolution) no longer holds what the rule settled.
    monkeypatch.setattr(
        hr.hyp_repo, "list_hypotheses", lambda conn, **k: [h for h in all_active if h["id"] not in resolved_ids]
    )
    monkeypatch.setattr(eod_review, "gather_symbol_context", lambda conn, symbols: {"symbols": {}})
    monkeypatch.setattr(eod_review, "_optional_llm_enrich", lambda prompt, fallback: fallback)
    eod_actions: list[dict[str, Any]] = []
    eod_drafts: list[dict[str, Any]] = []
    monkeypatch.setattr(eod_review.action_repo, "insert_action", lambda conn, **kw: (eod_actions.append(kw) or {"id": f"aal_e{len(eod_actions)}"}))
    monkeypatch.setattr(eod_review.draft_repo, "insert_draft", lambda conn, **kw: (eod_drafts.append(kw) or {"id": f"drf_e{len(eod_drafts)}"}))

    result = eod_review.run_eod_review(_Conn(), dry_run=False)

    assert result["resolution"]["applied"] == ["h-win"]
    assert result["resolution"]["counts"]["validated"] == 1
    assert result["active_hypotheses"] == 2
    # The resolver's own briefing for h-win lands in the same draft table, then
    # the review drafts only what the rule could not settle.
    by_scope = {d["scope"]: d["payload"] for d in eod_drafts}
    assert [d["scope"] for d in eod_drafts] == ["h-win", "h-meh", "h-manual"]
    assert by_scope["h-win"]["auto_resolved"] is True
    meh = by_scope["h-meh"]
    assert meh["outcome"]["excess_return"] == 0.01
    assert meh["bullets"][0].startswith("Outcome rule:")
    assert "dead band" in meh["rationale"]
    assert "outcome" not in by_scope["h-manual"]
    assert "auto_resolved" not in by_scope["h-manual"]
