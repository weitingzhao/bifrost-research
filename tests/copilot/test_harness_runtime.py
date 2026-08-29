"""Wave A+O+Y.1+Y.2+Y.3 — harness plan + run_objective branches."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from bifrost_research.copilot.harness.order_intent_schema import OrderIntent
from bifrost_research.copilot.harness.runtime import (
    _apply_hit_rate_gate,
    _lenses_from_flag_filter,
    _plan_for_objective,
    _policy_suggestion_from_plan,
    run_objective,
)


def test_plan_for_objective_returns_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIFROST_HARNESS_LLM_PLAN", raising=False)
    plan = _plan_for_objective(
        {
            "id": "obj-test",
            "persona": "loop_curator",
            "policy_json": {
                "max_candidates": 5,
                "preset": "momentum",
                "flag_filter": "iv_rank:hot",
            },
        }
    )
    assert "steps" in plan
    ops = [s.get("op") for s in plan["steps"]]
    assert "scan_universe" in ops
    assert "signal_decay_check" in ops
    assert "propose_candidates" in ops
    assert "await_approval" in ops
    assert plan["persona"] == "loop_curator"
    assert plan["policy"]["max_candidates"] == 5
    assert plan["generated_by"] == "heuristic"
    assert plan["fallback_reason"].startswith("llm_disabled")
    decay_note = next(
        s["note"] for s in plan["steps"] if s.get("op") == "signal_decay_check"
    )
    assert "not yet gating" not in decay_note
    assert "gate" in decay_note.lower()
    scan_note = next(s["note"] for s in plan["steps"] if s.get("op") == "scan_universe")
    assert "preset=momentum" in scan_note
    assert "weights" in scan_note


def test_plan_for_objective_uses_llm_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Y.2: policy.use_llm_plan=true + mock generate_plan_llm → generated_by=llm."""
    from bifrost_research.copilot.harness import runtime as rt

    monkeypatch.setattr(
        rt.plan_llm,
        "generate_plan_llm",
        lambda obj: {
            "steps": [
                {"op": "scan_universe", "note": "llm-picked scan"},
                {"op": "propose_candidates", "note": "llm-picked propose"},
                {"op": "await_approval", "note": "llm-picked approval"},
            ],
            "reasoning": "LLM decided to skip decay check for narrow universe.",
            "policy_suggestion": {"min_composite_score": 65.0},
            "llm_model": "deepseek-reasoner",
        },
    )
    plan = _plan_for_objective(
        {
            "id": "obj-llm",
            "persona": "loop_curator",
            "policy_json": {"use_llm_plan": True, "max_candidates": 3},
        }
    )
    assert plan["generated_by"] == "llm"
    assert plan["llm_model"] == "deepseek-reasoner"
    assert plan["llm_reasoning"].startswith("LLM decided")
    assert plan["policy_suggestion"] == {"min_composite_score": 65.0}
    ops = [s.get("op") for s in plan["steps"]]
    assert "signal_decay_check" not in ops  # LLM dropped it
    assert "propose_candidates" in ops
    assert "await_approval" in ops
    assert "fallback_reason" not in plan


def test_plan_for_objective_falls_back_when_llm_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Y.2: LLM enabled but generate_plan_llm returns None → heuristic + fallback_reason."""
    from bifrost_research.copilot.harness import runtime as rt

    monkeypatch.setattr(rt.plan_llm, "generate_plan_llm", lambda obj: None)
    plan = _plan_for_objective(
        {
            "id": "obj-fallback",
            "persona": "loop_curator",
            "policy_json": {"use_llm_plan": True, "max_candidates": 2},
        }
    )
    assert plan["generated_by"] == "heuristic"
    assert plan["fallback_reason"] == "llm_call_failed_or_invalid"
    ops = [s.get("op") for s in plan["steps"]]
    assert set(ops) == {
        "scan_universe",
        "signal_decay_check",
        "propose_candidates",
        "await_approval",
    }


def test_order_intent_to_payload_advisory_d10() -> None:
    intent = OrderIntent(
        hypothesis_id="hyp-1",
        strategy_template="iron_condor",
        rationale="test",
    )
    payload = intent.to_payload()
    assert payload["advisory"] is True
    assert payload["d10"] == "BLOCKED"
    assert payload["hypothesis_id"] == "hyp-1"


# --------------- run_objective branches (Y.1) ------------------


@pytest.fixture
def fake_conn() -> Any:
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


def _patch_repos(monkeypatch: pytest.MonkeyPatch, *, run_id: str = "run_1") -> dict[str, list]:
    captured: dict[str, list] = {
        "finish_status": [],
        "candidate_syms": [],
        "draft_payloads": [],
        "draft_kinds": [],
        "action_kinds": [],
    }

    from bifrost_research.copilot.harness import runtime as rt

    monkeypatch.setattr(
        rt.obj_repo,
        "create_run",
        lambda conn, objective_id, plan_json: {"id": run_id, "objective_id": objective_id},
    )

    def _finish(conn, rid, *, status, trace_json, outputs):
        captured["finish_status"].append((rid, status, outputs))
        return {"id": rid, "status": status, "outputs": outputs}

    monkeypatch.setattr(rt.obj_repo, "finish_run", _finish)

    counter = {"n": 0}

    def _create_candidate(conn, *, symbol, source, lens_snapshot, tags, source_ref):
        counter["n"] += 1
        captured["candidate_syms"].append((symbol, lens_snapshot.get("data_source"), tags))
        return {"id": f"cand_{counter['n']}", "symbol": symbol}

    monkeypatch.setattr(rt.cand_repo, "create_candidate", _create_candidate)

    action_counter = {"n": 0}

    def _insert_action(conn, **k):
        action_counter["n"] += 1
        captured["action_kinds"].append(k.get("action_kind"))
        return {"id": f"aal_{action_counter['n']}", **k}

    monkeypatch.setattr(rt.action_repo, "insert_action", _insert_action)

    def _insert_draft(conn, **k):
        captured["draft_payloads"].append(k["payload"])
        captured["draft_kinds"].append(k.get("kind"))
        return {"id": f"aid_{len(captured['draft_kinds'])}", **k}

    monkeypatch.setattr(rt.draft_repo, "insert_draft", _insert_draft)

    return captured


def test_run_objective_uses_scan_when_available(
    monkeypatch: pytest.MonkeyPatch, fake_conn: Any
) -> None:
    from bifrost_research.copilot.harness import runtime as rt

    monkeypatch.setattr(
        rt.ds,
        "top_scan_symbols",
        lambda conn, **k: [
            {"symbol": "AAPL", "composite_score": 82.0, "trade_date": None, "iv_rank_1y": 0.9},
            {"symbol": "MSFT", "composite_score": 78.0, "trade_date": None, "iv_rank_1y": 0.85},
        ],
    )
    monkeypatch.setattr(
        rt.ds,
        "global_signal_decay_summary",
        lambda conn, **k: {"iv_rank": {"n": 20, "hit_rate_5d": 0.6, "hit_rate_20d": 0.7}},
    )
    captured = _patch_repos(monkeypatch)

    result = run_objective(
        fake_conn,
        objective={
            "id": "obj-a",
            "title": "Scan Path",
            "policy_json": {"max_candidates": 2, "flag_filter": "iv_rank:hot"},
        },
    )
    assert result["outputs"]["data_source"] == "scan"
    assert result["outputs"]["candidate_ids"] == ["cand_1", "cand_2"]
    assert captured["finish_status"][-1][1] == "awaiting_approval"
    # candidate uses data_source=scan tag
    assert all(
        ("scan" in tags) for _sym, _src, tags in captured["candidate_syms"]
    )
    # draft payload embeds signal_decay
    assert captured["draft_payloads"][-1]["signal_decay"]["iv_rank"]["hit_rate_5d"] == 0.6


def test_run_objective_falls_back_to_seed_symbols(
    monkeypatch: pytest.MonkeyPatch, fake_conn: Any
) -> None:
    from bifrost_research.copilot.harness import runtime as rt

    monkeypatch.setattr(rt.ds, "top_scan_symbols", lambda conn, **k: [])
    monkeypatch.setattr(
        rt.ds,
        "global_signal_decay_summary",
        lambda conn, **k: {},
    )
    captured = _patch_repos(monkeypatch)

    result = run_objective(
        fake_conn,
        objective={
            "id": "obj-b",
            "title": "Fallback Path",
            "policy_json": {"max_candidates": 3, "seed_symbols": ["nvda", "amd", "tsla"]},
        },
    )
    assert result["outputs"]["data_source"] == "fallback_seed_symbols"
    assert [s for s, _src, _tags in captured["candidate_syms"]] == ["NVDA", "AMD", "TSLA"]
    assert captured["finish_status"][-1][1] == "awaiting_approval"
    # fallback tag present
    assert all(
        "fallback_seed_symbols" in tags for _sym, _src, tags in captured["candidate_syms"]
    )


def test_run_objective_failed_when_both_sources_empty(
    monkeypatch: pytest.MonkeyPatch, fake_conn: Any
) -> None:
    from bifrost_research.copilot.harness import runtime as rt

    monkeypatch.setattr(rt.ds, "top_scan_symbols", lambda conn, **k: [])
    monkeypatch.setattr(rt.ds, "global_signal_decay_summary", lambda conn, **k: {})
    captured = _patch_repos(monkeypatch)

    result = run_objective(
        fake_conn,
        objective={"id": "obj-c", "title": "Empty", "policy_json": {"max_candidates": 3}},
    )
    assert result["outputs"]["data_source"] == "none"
    assert result["outputs"]["candidate_ids"] == []
    assert captured["finish_status"][-1][1] == "failed"
    # no candidate created; no draft inserted
    assert captured["candidate_syms"] == []
    assert captured["draft_payloads"] == []
    # Y.3: gate output is still recorded even on no-data failure
    assert "hit_rate_gate" in result["outputs"]


# --------------- Wave Y.3: helpers ---------------------------------------


class TestLensesFromFlagFilter:
    def test_empty(self) -> None:
        assert _lenses_from_flag_filter(None) == []
        assert _lenses_from_flag_filter("") == []
        assert _lenses_from_flag_filter("   ") == []

    def test_single(self) -> None:
        assert _lenses_from_flag_filter("iv_rank:hot") == ["iv_rank"]

    def test_multiple_dedup_order(self) -> None:
        assert _lenses_from_flag_filter("iv_rank:hot,vrp:hot,iv_rank:warm") == [
            "iv_rank",
            "vrp",
        ]

    def test_ignores_bad_pieces(self) -> None:
        assert _lenses_from_flag_filter("iv_rank,vrp:hot,:bad") == ["vrp"]


class TestHitRateGate:
    def _decay(self, **overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
        base = {
            "iv_rank": {"n": 20, "hit_rate_5d": 0.5, "hit_rate_20d": 0.6},
            "vrp": {"n": 15, "hit_rate_5d": 0.55, "hit_rate_20d": 0.62},
            "opex_pin": {"n": 10, "hit_rate_5d": 0.4, "hit_rate_20d": 0.45},
        }
        base.update(overrides)  # type: ignore[arg-type]
        return base

    def test_gate_not_applied_without_min_hit_rate(self) -> None:
        g = _apply_hit_rate_gate(
            policy={"flag_filter": "iv_rank:hot"},
            decay_summary=self._decay(),
            flag_filter="iv_rank:hot",
        )
        assert g["applied"] is False
        assert g["ok"] is True
        assert "unset" in g["reason"]

    def test_gate_not_applied_without_flag_filter(self) -> None:
        g = _apply_hit_rate_gate(
            policy={"min_hit_rate": 0.55},
            decay_summary=self._decay(),
            flag_filter=None,
        )
        assert g["applied"] is False
        assert "flag_filter" in g["reason"]

    def test_gate_not_applied_when_min_hit_rate_not_number(self) -> None:
        g = _apply_hit_rate_gate(
            policy={"min_hit_rate": "not-a-number"},
            decay_summary=self._decay(),
            flag_filter="iv_rank:hot",
        )
        assert g["applied"] is False
        assert "not a number" in g["reason"]

    def test_gate_pass(self) -> None:
        g = _apply_hit_rate_gate(
            policy={"min_hit_rate": 0.55},
            decay_summary=self._decay(),
            flag_filter="iv_rank:hot,vrp:hot",
        )
        assert g["applied"] is True
        assert g["ok"] is True
        assert g["failing"] == []
        assert g["lens_results"]["iv_rank"]["pass"] is True
        assert g["lens_results"]["vrp"]["pass"] is True

    def test_gate_fail_on_low_hit_rate(self) -> None:
        g = _apply_hit_rate_gate(
            policy={"min_hit_rate": 0.55},
            decay_summary=self._decay(),
            flag_filter="opex_pin:hot",
        )
        assert g["applied"] is True
        assert g["ok"] is False
        assert g["failing"] == ["opex_pin"]

    def test_unmapped_flag_is_skipped_not_failing(self) -> None:
        g = _apply_hit_rate_gate(
            policy={"min_hit_rate": 0.55},
            decay_summary=self._decay(),
            flag_filter="nonexistent:hot",
        )
        assert g["applied"] is True
        assert g["ok"] is True
        assert g["failing"] == []
        assert any(
            s["flag_key"] == "nonexistent" and s["reason"] == "unmapped"
            for s in g["skipped"]
        )

    def test_mapped_lens_without_decay_row_skipped(self) -> None:
        decay = self._decay(iv_rank={"n": 0, "hit_rate_20d": 0.99})
        g = _apply_hit_rate_gate(
            policy={"min_hit_rate": 0.55},
            decay_summary=decay,
            flag_filter="iv_rank:hot",
        )
        assert g["applied"] is True
        assert g["ok"] is True
        assert g["failing"] == []
        assert any(
            s["flag_key"] == "iv_rank" and s["reason"] == "no_decay_row"
            for s in g["skipped"]
        )

    def test_unmapped_atm_slope_does_not_join_failing(self) -> None:
        g = _apply_hit_rate_gate(
            policy={"min_hit_rate": 0.55},
            decay_summary={"iv_rank": {"n": 20, "hit_rate_20d": 0.7}},
            flag_filter="atm_slope:hot,iv_rank:hot",
        )
        assert g["applied"] is True
        assert g["ok"] is True
        assert "atm_slope" not in g["failing"]
        assert g["failing"] == []
        assert g["lens_results"]["iv_rank"]["pass"] is True
        assert any(
            s["flag_key"] == "atm_slope" and s["reason"] == "unmapped"
            for s in g["skipped"]
        )

    def test_pin_flag_maps_to_opex_pin(self) -> None:
        g = _apply_hit_rate_gate(
            policy={"min_hit_rate": 0.55},
            decay_summary=self._decay(),
            flag_filter="pin:hot",
        )
        assert g["applied"] is True
        assert g["ok"] is False
        assert g["failing"] == ["opex_pin"]
        assert g["lens_results"]["opex_pin"]["flag_key"] == "pin"


class TestPolicySuggestionFromPlan:
    def test_no_suggestion(self) -> None:
        assert (
            _policy_suggestion_from_plan(
                {"policy_suggestion": None}, {"max_candidates": 3}
            )
            is None
        )

    def test_empty_dict_returns_none(self) -> None:
        assert _policy_suggestion_from_plan({"policy_suggestion": {}}, {}) is None

    def test_only_no_op_keys_returns_none(self) -> None:
        # keys outside whitelist are dropped by plan_llm; simulate same
        diff = _policy_suggestion_from_plan(
            {"policy_suggestion": {"random_field": "x"}}, {}
        )
        assert diff is None

    def test_all_keys_match_current_returns_none(self) -> None:
        diff = _policy_suggestion_from_plan(
            {"policy_suggestion": {"min_hit_rate": 0.6}},
            {"min_hit_rate": 0.6},
        )
        assert diff is None

    def test_diff_picks_only_changed(self) -> None:
        diff = _policy_suggestion_from_plan(
            {
                "policy_suggestion": {
                    "min_hit_rate": 0.65,
                    "preset": "momentum",  # unchanged
                }
            },
            {"min_hit_rate": 0.55, "preset": "momentum"},
        )
        assert diff == {"min_hit_rate": 0.65}


# --------------- Wave Y.3: run_objective integration branches -----------


def test_run_objective_gate_pass_no_warn(
    monkeypatch: pytest.MonkeyPatch, fake_conn: Any
) -> None:
    from bifrost_research.copilot.harness import runtime as rt

    monkeypatch.setattr(
        rt.ds,
        "top_scan_symbols",
        lambda conn, **k: [
            {"symbol": "AAPL", "composite_score": 82.0, "trade_date": None},
        ],
    )
    monkeypatch.setattr(
        rt.ds,
        "global_signal_decay_summary",
        lambda conn, **k: {
            "iv_rank": {"n": 20, "hit_rate_20d": 0.62, "hit_rate_5d": 0.5},
        },
    )
    captured = _patch_repos(monkeypatch)

    result = run_objective(
        fake_conn,
        objective={
            "id": "obj-gp",
            "title": "Gate pass",
            "policy_json": {
                "max_candidates": 1,
                "flag_filter": "iv_rank:hot",
                "min_hit_rate": 0.55,
            },
        },
    )
    assert result["outputs"]["hit_rate_gate"]["applied"] is True
    assert result["outputs"]["hit_rate_gate"]["ok"] is True
    # candidate_batch payload should NOT carry the warn flag
    cb_payload = next(
        p for p, k in zip(captured["draft_payloads"], captured["draft_kinds"], strict=True)
        if k == "candidate_batch"
    )
    assert "hit_rate_warn" not in cb_payload


def test_run_objective_gate_fail_carries_warn(
    monkeypatch: pytest.MonkeyPatch, fake_conn: Any
) -> None:
    """Y.3 C3: gate failure does NOT abort; run stays awaiting_approval + warn tag."""
    from bifrost_research.copilot.harness import runtime as rt

    monkeypatch.setattr(
        rt.ds,
        "top_scan_symbols",
        lambda conn, **k: [
            {"symbol": "AAPL", "composite_score": 82.0, "trade_date": None},
        ],
    )
    monkeypatch.setattr(
        rt.ds,
        "global_signal_decay_summary",
        lambda conn, **k: {
            "iv_rank": {"n": 20, "hit_rate_20d": 0.30, "hit_rate_5d": 0.25},
        },
    )
    captured = _patch_repos(monkeypatch)

    result = run_objective(
        fake_conn,
        objective={
            "id": "obj-gf",
            "title": "Gate fail",
            "policy_json": {
                "max_candidates": 1,
                "flag_filter": "iv_rank:hot",
                "min_hit_rate": 0.55,
            },
        },
    )
    # Run still succeeds (C3)
    assert captured["finish_status"][-1][1] == "awaiting_approval"
    assert result["outputs"]["candidate_ids"] == ["cand_1"]
    # Gate captured in outputs
    assert result["outputs"]["hit_rate_gate"]["ok"] is False
    assert result["outputs"]["hit_rate_gate"]["failing"] == ["iv_rank"]
    # candidate_batch draft carries warn
    cb_payload = next(
        p for p, k in zip(captured["draft_payloads"], captured["draft_kinds"], strict=True)
        if k == "candidate_batch"
    )
    assert cb_payload["hit_rate_warn"] is True
    assert cb_payload["hit_rate_gate"]["failing"] == ["iv_rank"]


def test_run_objective_creates_policy_suggestion_draft_from_llm_plan(
    monkeypatch: pytest.MonkeyPatch, fake_conn: Any
) -> None:
    """Y.3 A1: LLM plan with non-trivial policy_suggestion → separate draft."""
    from bifrost_research.copilot.harness import runtime as rt

    # LLM plan with actionable suggestion
    monkeypatch.setattr(
        rt.plan_llm,
        "generate_plan_llm",
        lambda obj: {
            "steps": [
                {"op": "scan_universe", "note": "llm"},
                {"op": "propose_candidates", "note": "llm"},
                {"op": "await_approval", "note": "llm"},
            ],
            "reasoning": "raise min_hit_rate for higher confidence.",
            "policy_suggestion": {"min_hit_rate": 0.7, "max_candidates": 5},
            "llm_model": "deepseek-reasoner",
        },
    )
    monkeypatch.setattr(
        rt.ds,
        "top_scan_symbols",
        lambda conn, **k: [{"symbol": "MSFT", "composite_score": 90.0, "trade_date": None}],
    )
    monkeypatch.setattr(rt.ds, "global_signal_decay_summary", lambda conn, **k: {})
    captured = _patch_repos(monkeypatch)

    result = run_objective(
        fake_conn,
        objective={
            "id": "obj-ps",
            "title": "Policy suggestion",
            "policy_json": {
                "use_llm_plan": True,
                "max_candidates": 3,
                "min_hit_rate": 0.55,
            },
        },
    )
    # Two drafts: candidate_batch + policy_suggestion
    assert captured["draft_kinds"] == ["candidate_batch", "policy_suggestion"]
    ps_payload = captured["draft_payloads"][1]
    assert ps_payload["suggestion"] == {"min_hit_rate": 0.7, "max_candidates": 5}
    assert ps_payload["source"] == "harness_llm_plan"
    assert ps_payload["llm_model"] == "deepseek-reasoner"
    assert result["outputs"]["policy_suggestion_draft_id"] == "aid_2"
    # Both actions inserted
    assert "harness_candidate_batch" in captured["action_kinds"]
    assert "harness_policy_suggestion" in captured["action_kinds"]


def test_run_objective_no_policy_suggestion_draft_when_diff_empty(
    monkeypatch: pytest.MonkeyPatch, fake_conn: Any
) -> None:
    """LLM proposes only fields matching current policy → no draft."""
    from bifrost_research.copilot.harness import runtime as rt

    monkeypatch.setattr(
        rt.plan_llm,
        "generate_plan_llm",
        lambda obj: {
            "steps": [
                {"op": "propose_candidates", "note": "llm"},
                {"op": "await_approval", "note": "llm"},
            ],
            "reasoning": None,
            "policy_suggestion": {"min_hit_rate": 0.55},  # same as current
            "llm_model": "deepseek-reasoner",
        },
    )
    monkeypatch.setattr(
        rt.ds,
        "top_scan_symbols",
        lambda conn, **k: [{"symbol": "SPY", "composite_score": 75.0, "trade_date": None}],
    )
    monkeypatch.setattr(rt.ds, "global_signal_decay_summary", lambda conn, **k: {})
    captured = _patch_repos(monkeypatch)

    result = run_objective(
        fake_conn,
        objective={
            "id": "obj-ns",
            "title": "No suggestion diff",
            "policy_json": {"use_llm_plan": True, "min_hit_rate": 0.55},
        },
    )
    assert captured["draft_kinds"] == ["candidate_batch"]
    assert result["outputs"]["policy_suggestion_draft_id"] is None


def test_run_objective_no_policy_suggestion_draft_on_heuristic_plan(
    monkeypatch: pytest.MonkeyPatch, fake_conn: Any
) -> None:
    """Heuristic plan carries no policy_suggestion → no separate draft."""
    from bifrost_research.copilot.harness import runtime as rt

    monkeypatch.delenv("BIFROST_HARNESS_LLM_PLAN", raising=False)
    monkeypatch.setattr(
        rt.ds,
        "top_scan_symbols",
        lambda conn, **k: [{"symbol": "SPY", "composite_score": 75.0, "trade_date": None}],
    )
    monkeypatch.setattr(rt.ds, "global_signal_decay_summary", lambda conn, **k: {})
    captured = _patch_repos(monkeypatch)

    run_objective(
        fake_conn,
        objective={"id": "obj-h", "title": "Heuristic", "policy_json": {}},
    )
    assert captured["draft_kinds"] == ["candidate_batch"]
