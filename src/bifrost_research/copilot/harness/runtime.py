"""Harness runtime — Wave A + Y + LS-2 Stock-first universe."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from bifrost_research.copilot.harness import data_sources as ds
from bifrost_research.copilot.harness import plan_llm
from bifrost_research.copilot.harness.gate import (
    apply_hit_rate_gate,
    lenses_from_flag_filter,
)
from bifrost_research.copilot.harness.policy_schema import LoopPolicy, parse_policy
from bifrost_research.copilot.harness.suggestion import policy_suggestion_from_plan
from bifrost_research.copilot.harness.universe.registry import resolve_universe
from bifrost_research.repositories import ai_action_log as action_repo
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import candidate_pool as cand_repo
from bifrost_research.repositories import objective as obj_repo

_apply_hit_rate_gate = apply_hit_rate_gate
_lenses_from_flag_filter = lenses_from_flag_filter
_policy_suggestion_from_plan = policy_suggestion_from_plan

logger = logging.getLogger(__name__)


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def _scan_universe_note(loop_policy: LoopPolicy) -> str:
    mode = loop_policy.universe_mode
    if mode == "scan_legacy":
        return (
            f"Read features.stock_signal_scan_daily preset={loop_policy.preset} "
            f"(flag_filter={loop_policy.flag_filter_str() or 'none'})"
        )
    if mode == "stock_composite":
        layers = loop_policy.layers
        return (
            f"Stock composite funnel: SEPA path={layers.sepa.stage} min_score={layers.sepa.min_score}; "
            f"momentum required={layers.momentum.required}; events required={layers.events.required}; "
            f"option_overlay enabled={loop_policy.option_overlay.enabled} "
            f"required={loop_policy.option_overlay.required}"
        )
    return f"Universe mode {mode} from Golden Source stock features"


def _heuristic_plan(objective: dict[str, Any]) -> dict[str, Any]:
    """Static 4-step template used as fallback when the LLM plan is unavailable."""
    policy_raw = objective.get("policy_json") or {}
    loop_policy = parse_policy(policy_raw)
    max_candidates = loop_policy.max_candidates
    decay_note = (
        "Fetch global lens hit-rate summary (scan_legacy only)"
        if loop_policy.universe_mode == "scan_legacy"
        else "Skipped in stock-first modes — option lens gate not applied"
    )
    steps: list[dict[str, Any]] = [
        {"op": "scan_universe", "note": _scan_universe_note(loop_policy)},
    ]
    if loop_policy.universe_mode == "scan_legacy":
        steps.append(
            {
                "op": "signal_decay_check",
                "note": (
                    "Fetch global lens hit-rate summary; min_hit_rate + "
                    "flag_filter apply the Y.3 hit-rate gate"
                ),
            }
        )
    else:
        steps.append({"op": "signal_decay_check", "note": decay_note})
    steps.extend(
        [
            {
                "op": "propose_candidates",
                "max": max_candidates,
                "note": "Propose top candidates into pool + draft (universe first; fallback seed_symbols)",
            },
            {"op": "await_approval", "note": "Owner approves drafts in Decision Inbox"},
        ]
    )
    return {
        "steps": steps,
        "persona": objective.get("persona") or "loop_curator",
        "policy": policy_raw,
        "generated_by": "heuristic",
    }


def _plan_for_objective(objective: dict[str, Any]) -> dict[str, Any]:
    heuristic = _heuristic_plan(objective)
    policy = objective.get("policy_json") or {}
    enabled, reason = plan_llm.is_llm_plan_enabled(policy)
    if not enabled:
        heuristic["fallback_reason"] = f"llm_disabled: {reason}"
        return heuristic

    llm_result = plan_llm.generate_plan_llm(objective)
    if not llm_result:
        heuristic["fallback_reason"] = "llm_call_failed_or_invalid"
        return heuristic

    return {
        "steps": llm_result["steps"],
        "persona": objective.get("persona") or "loop_curator",
        "policy": policy,
        "generated_by": "llm",
        "llm_model": llm_result.get("llm_model"),
        "llm_reasoning": llm_result.get("reasoning"),
        "policy_suggestion": llm_result.get("policy_suggestion"),
    }


def _primary_score(meta: dict[str, Any] | None) -> float | None:
    if not meta:
        return None
    for key in ("sepa_score", "option_composite", "composite_score", "momentum_score", "score"):
        val = meta.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def _build_lens_snapshot(
    *,
    objective_id: str,
    run_id: str,
    data_source: str,
    meta: dict[str, Any] | None,
) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "objective_id": objective_id,
        "run_id": run_id,
        "data_source": data_source,
    }
    if not meta:
        return snap
    for key in (
        "sepa_score",
        "momentum_score",
        "score",
        "option_composite",
        "composite_score",
        "iv_rank_1y",
        "vrp_pct_252d",
        "terrain_regime",
        "lens_flags",
        "event_importance",
        "grade",
        "path",
        "stage",
        "trade_date",
        "event_date",
    ):
        if key in meta and meta[key] is not None:
            val = meta[key]
            if hasattr(val, "isoformat"):
                snap[key] = val.isoformat()
            else:
                snap[key] = val
    return snap


def run_objective(conn: _Connection, *, objective: dict[str, Any]) -> dict[str, Any]:
    """Execute a harness run: universe → decay (legacy) → propose."""
    plan = _plan_for_objective(objective)
    run = obj_repo.create_run(conn, objective_id=objective["id"], plan_json=plan)
    run_id = run["id"]
    trace: list[dict[str, Any]] = []
    candidate_ids: list[str] = []
    draft_ids: list[str] = []

    try:
        policy_raw = objective.get("policy_json") or {}
        loop_policy = parse_policy(policy_raw)
        seed_symbols = [s.strip().upper() for s in loop_policy.seed_symbols if s.strip()]
        max_n = max(1, min(loop_policy.max_candidates, 50))
        source_label = loop_policy.source or "harness"
        flag_filter = loop_policy.flag_filter_str()

        trace.append({"step": "plan", "plan": plan})

        # 1. Resolve universe --------------------------------------------------
        universe = resolve_universe(conn, loop_policy, limit=max_n)
        universe_symbols = list(universe.symbols)
        row_meta_by_symbol = dict(universe.row_meta_by_symbol)

        trace.append(
            {
                "step": "scan_universe",
                "returned": len(universe_symbols),
                "symbols": universe_symbols,
                "universe_mode": universe.universe_mode,
                "funnel": universe.funnel_dicts(),
                "layer_results": universe.layer_results,
                "option_overlay_applied": universe.option_overlay_applied,
                "policy_warnings": universe.policy_warnings,
                "flag_filter": flag_filter if loop_policy.universe_mode == "scan_legacy" else None,
                "min_composite_score": loop_policy.min_composite_score,
                "preset": loop_policy.preset,
            }
        )

        # 2. Signal decay (scan_legacy only) -----------------------------------
        if loop_policy.universe_mode == "scan_legacy":
            decay_summary = ds.global_signal_decay_summary(conn)
            trace.append({"step": "signal_decay_check", "summary": decay_summary})
            gate = apply_hit_rate_gate(
                policy=policy_raw, decay_summary=decay_summary, flag_filter=flag_filter
            )
        else:
            decay_summary = {}
            gate = {
                "applied": False,
                "ok": True,
                "skipped": True,
                "reason": "stock-first mode — option lens hit-rate gate not applied",
            }
            trace.append({"step": "signal_decay_check", **gate})
        trace.append({"step": "hit_rate_gate", **gate})

        # 3. Choose data source ------------------------------------------------
        if universe_symbols:
            chosen_symbols = universe_symbols[:max_n]
            data_source = universe.data_source
        elif seed_symbols:
            chosen_symbols = seed_symbols[:max_n]
            data_source = "fallback_seed_symbols"
            row_meta_by_symbol = {}
            logger.info("harness run %s: universe empty; falling back to seed_symbols", run_id)
        else:
            data_source = "none"
            row_meta_by_symbol = {}
            chosen_symbols = []

        if not chosen_symbols:
            trace.append(
                {
                    "step": "no_data",
                    "reason": "universe empty and no seed_symbols configured",
                }
            )
            empty_outputs = {
                "candidate_ids": [],
                "hypothesis_ids": [],
                "decision_draft_ids": [],
                "draft_ids": [],
                "data_source": data_source,
                "universe_mode": loop_policy.universe_mode,
                "policy_suggestion_draft_id": None,
                "hit_rate_gate": gate,
            }
            finished = obj_repo.finish_run(
                conn,
                run_id,
                status="failed",
                trace_json={"events": trace},
                outputs=empty_outputs,
            )
            return {
                "run": finished,
                "outputs": empty_outputs,
                "advisory": "D10 BLOCKED — harness proposes only; no order placement.",
            }

        trace.append({"step": "resolved_source", "data_source": data_source})

        # 4. Propose candidates ------------------------------------------------
        proposed_items: list[dict[str, Any]] = []
        for sym in chosen_symbols:
            sym_meta = row_meta_by_symbol.get(sym)
            lens_snapshot = _build_lens_snapshot(
                objective_id=objective["id"],
                run_id=run_id,
                data_source=data_source,
                meta=sym_meta,
            )
            row = cand_repo.create_candidate(
                conn,
                symbol=sym,
                source=source_label,
                lens_snapshot=lens_snapshot,
                tags=["harness", data_source],
                source_ref={"objective_id": objective["id"], "run_id": run_id},
            )
            candidate_ids.append(row["id"])
            proposed_items.append(
                {
                    "id": row["id"],
                    "symbol": row["symbol"],
                    "score": _primary_score(sym_meta),
                }
            )
            trace.append({"step": "propose_candidate", "symbol": row["symbol"], "id": row["id"]})

        # 5. Draft candidate_batch --------------------------------------------
        action = action_repo.insert_action(
            conn,
            action_kind="harness_candidate_batch",
            action_source="harness",
            input_payload={
                "objective_id": objective["id"],
                "run_id": run_id,
                "data_source": data_source,
                "universe_mode": loop_policy.universe_mode,
            },
            output_payload={"candidate_ids": candidate_ids},
            status="proposed",
        )
        candidate_payload: dict[str, Any] = {
            "objective_id": objective["id"],
            "run_id": run_id,
            "items": proposed_items,
            "title": objective.get("title"),
            "description": objective.get("description"),
            "data_source": data_source,
            "universe_mode": loop_policy.universe_mode,
            "funnel": universe.funnel_dicts(),
            "signal_decay": decay_summary,
            "hit_rate_gate": gate,
        }
        if gate.get("applied") and not gate.get("ok"):
            candidate_payload["hit_rate_warn"] = True
        draft = draft_repo.insert_draft(
            conn,
            kind="candidate_batch",
            payload=candidate_payload,
            scope=f"objective:{objective['id']}",
            generated_by="harness",
            linked_action_id=action["id"],
        )
        draft_ids.append(draft["id"])
        trace.append({"step": "draft_candidate_batch", "draft_id": draft["id"]})

        policy_suggestion_draft_id: str | None = None
        suggestion_diff = policy_suggestion_from_plan(plan, policy_raw)
        if suggestion_diff:
            ps_action = action_repo.insert_action(
                conn,
                action_kind="harness_policy_suggestion",
                action_source="harness",
                input_payload={
                    "objective_id": objective["id"],
                    "run_id": run_id,
                    "source_plan_generated_by": plan.get("generated_by"),
                },
                output_payload={"suggestion": suggestion_diff},
                status="proposed",
            )
            ps_draft = draft_repo.insert_draft(
                conn,
                kind="policy_suggestion",
                payload={
                    "objective_id": objective["id"],
                    "run_id": run_id,
                    "suggestion": suggestion_diff,
                    "current_policy": policy_raw,
                    "source": "harness_llm_plan",
                    "llm_model": plan.get("llm_model"),
                    "llm_reasoning": plan.get("llm_reasoning"),
                },
                scope=f"objective:{objective['id']}",
                generated_by="harness",
                linked_action_id=ps_action["id"],
            )
            policy_suggestion_draft_id = ps_draft["id"]
            draft_ids.append(policy_suggestion_draft_id)
            trace.append(
                {
                    "step": "draft_policy_suggestion",
                    "draft_id": policy_suggestion_draft_id,
                    "diff": suggestion_diff,
                }
            )

        outputs = {
            "candidate_ids": candidate_ids,
            "hypothesis_ids": [],
            "decision_draft_ids": [],
            "draft_ids": draft_ids,
            "data_source": data_source,
            "universe_mode": loop_policy.universe_mode,
            "top_scan_symbols": universe_symbols,
            "top_symbols": universe_symbols,
            "policy_suggestion_draft_id": policy_suggestion_draft_id,
            "hit_rate_gate": gate,
        }
        finished = obj_repo.finish_run(
            conn,
            run_id,
            status="awaiting_approval",
            trace_json={"events": trace},
            outputs=outputs,
        )
        return {
            "run": finished,
            "outputs": outputs,
            "advisory": "D10 BLOCKED — harness proposes only; no order placement.",
        }
    except Exception as exc:
        logger.exception("harness run %s failed", run_id)
        obj_repo.finish_run(
            conn,
            run_id,
            status="failed",
            trace_json={"events": trace, "error": str(exc)},
            outputs={"candidate_ids": candidate_ids, "draft_ids": draft_ids},
        )
        raise
