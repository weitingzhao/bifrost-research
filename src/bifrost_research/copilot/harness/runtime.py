"""Harness runtime — Wave A Stage 3 + Wave Y.1/Y.2/Y.3 Loop Smartness.

Plan → scan → decay-check → gate → propose → wait_approval.

Data source priority (spine `D-Loop-Smartness-Y1`):
  1. `features.stock_signal_scan_daily` top-N by composite_score (+ optional flag filter)
  2. Fallback to policy.seed_symbols[:max_n] (heuristic) when scan is empty
  3. `status=failed` only when both sources yield zero symbols

Plan step (spine `D-Loop-Smartness-Y2`):
  * LLM-driven plan when env `BIFROST_HARNESS_LLM_PLAN=1` (or policy override);
    fail-soft to heuristic 4-step template on any error/timeout/schema failure.
  * Plan carries `generated_by = "llm" | "heuristic"` and optional
    `policy_suggestion` (advisory).

Hit-rate gate + suggestion loop (spine `D-Loop-Smartness-Y3`):
  * When `policy.min_hit_rate` and `policy.flag_filter` are both set, the
    filter-scoped lenses' `hit_rate_20d` from `global_signal_decay_summary`
    are checked (B3).  Failing lenses do NOT block the run (C3); the
    candidate_batch draft carries `hit_rate_warn: true` so Owner can
    override in Decision Inbox.
  * When the LLM plan yields a non-empty `policy_suggestion`, an
    independent ``policy_suggestion`` draft is inserted; Owner approves it
    to merge the suggestion onto ``objective.policy_json`` (A1).

D10 BLOCKED — never touches Trade write paths or the IB operator command stream.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from bifrost_research.copilot.harness import data_sources as ds
from bifrost_research.copilot.harness import plan_llm
from bifrost_research.copilot.harness.gate import (
    apply_hit_rate_gate,
    lenses_from_flag_filter,
)
from bifrost_research.copilot.harness.suggestion import policy_suggestion_from_plan
from bifrost_research.repositories import ai_action_log as action_repo
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import candidate_pool as cand_repo
from bifrost_research.repositories import objective as obj_repo

# Re-exports for backward compatibility — existing tests and callers may
# still import these private aliases from `runtime`.
_apply_hit_rate_gate = apply_hit_rate_gate
_lenses_from_flag_filter = lenses_from_flag_filter
_policy_suggestion_from_plan = policy_suggestion_from_plan

logger = logging.getLogger(__name__)


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def _heuristic_plan(objective: dict[str, Any]) -> dict[str, Any]:
    """Static 4-step template used as fallback when the LLM plan is unavailable."""
    policy = objective.get("policy_json") or {}
    max_candidates = int(policy.get("max_candidates") or 3)
    preset = str(policy.get("preset") or "neutral")
    flag_filter = policy.get("flag_filter") or None
    return {
        "steps": [
            {
                "op": "scan_universe",
                "note": (
                    f"Read features.stock_signal_scan_daily with preset={preset} "
                    f"weights (flag_filter={flag_filter or 'none'})"
                ),
            },
            {
                "op": "signal_decay_check",
                "note": (
                    "Fetch global lens hit-rate summary; min_hit_rate + "
                    "flag_filter apply the Y.3 hit-rate gate"
                ),
            },
            {
                "op": "propose_candidates",
                "max": max_candidates,
                "note": "Propose top candidates into pool + draft (scan first; fallback seed_symbols)",
            },
            {"op": "await_approval", "note": "Owner approves drafts in Decision Inbox"},
        ],
        "persona": objective.get("persona") or "loop_curator",
        "policy": policy,
        "generated_by": "heuristic",
    }


def _plan_for_objective(objective: dict[str, Any]) -> dict[str, Any]:
    """Choose LLM plan when enabled+valid; else fall back to heuristic template.

    LLM plan is always merged onto the heuristic scaffold so downstream code
    (persona, policy snapshot) stays stable regardless of LLM behavior.
    """
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


def _symbols_from_scan(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        sym = r.get("symbol")
        if not isinstance(sym, str):
            continue
        s = sym.strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def run_objective(conn: _Connection, *, objective: dict[str, Any]) -> dict[str, Any]:
    """Execute a harness run: scan → decay check → propose.

    Produces candidate rows and a candidate_batch draft for Owner approval.
    Ends with `awaiting_approval` on success, `failed` when both scan and
    seed_symbols yield zero symbols.
    """
    plan = _plan_for_objective(objective)
    run = obj_repo.create_run(conn, objective_id=objective["id"], plan_json=plan)
    run_id = run["id"]
    trace: list[dict[str, Any]] = []
    candidate_ids: list[str] = []
    draft_ids: list[str] = []

    try:
        policy = objective.get("policy_json") or {}
        seed_symbols_raw = policy.get("seed_symbols") or []
        seed_symbols = [str(s).strip().upper() for s in seed_symbols_raw if str(s).strip()]
        max_n = max(1, min(int(policy.get("max_candidates") or 3), 50))
        source_label = str(policy.get("source") or "harness")
        flag_filter = policy.get("flag_filter") or None
        preset = str(policy.get("preset") or "neutral")
        min_composite = policy.get("min_composite_score")
        try:
            min_composite_f = float(min_composite) if min_composite is not None else None
        except (TypeError, ValueError):
            min_composite_f = None

        trace.append({"step": "plan", "plan": plan})

        # 1. Scan universe -----------------------------------------------------
        scan_rows = ds.top_scan_symbols(
            conn,
            limit=max_n,
            flag_filter=flag_filter,
            min_composite_score=min_composite_f,
            preset=preset,
        )
        scan_symbols = _symbols_from_scan(scan_rows)
        trace.append(
            {
                "step": "scan_universe",
                "returned": len(scan_symbols),
                "symbols": scan_symbols,
                "flag_filter": flag_filter,
                "min_composite_score": min_composite_f,
                "preset": preset,
            }
        )

        # 2. Signal decay summary (advisory trace) -----------------------------
        decay_summary = ds.global_signal_decay_summary(conn)
        trace.append({"step": "signal_decay_check", "summary": decay_summary})

        # 2b. Hit-rate gate (Y.3 B3+C3) ---------------------------------------
        gate = apply_hit_rate_gate(
            policy=policy, decay_summary=decay_summary, flag_filter=flag_filter
        )
        trace.append({"step": "hit_rate_gate", **gate})

        # 3. Choose data source (scan first; else seed_symbols; else failed) ---
        if scan_symbols:
            chosen_symbols = scan_symbols[:max_n]
            data_source = "scan"
            row_meta_by_symbol: dict[str, dict[str, Any]] = {}
            for r in scan_rows:
                sym = r.get("symbol")
                if isinstance(sym, str):
                    row_meta_by_symbol[sym.strip().upper()] = r
        elif seed_symbols:
            chosen_symbols = seed_symbols[:max_n]
            data_source = "fallback_seed_symbols"
            row_meta_by_symbol = {}
            logger.info("harness run %s: scan empty; falling back to seed_symbols", run_id)
        else:
            data_source = "none"
            row_meta_by_symbol = {}
            chosen_symbols = []

        if not chosen_symbols:
            trace.append(
                {
                    "step": "no_data",
                    "reason": "scan empty and no seed_symbols configured",
                }
            )
            empty_outputs = {
                "candidate_ids": [],
                "hypothesis_ids": [],
                "decision_draft_ids": [],
                "draft_ids": [],
                "data_source": data_source,
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
            scan_meta = row_meta_by_symbol.get(sym)
            lens_snapshot: dict[str, Any] = {
                "objective_id": objective["id"],
                "run_id": run_id,
                "data_source": data_source,
            }
            if scan_meta:
                lens_snapshot.update(
                    {
                        "composite_score": scan_meta.get("composite_score"),
                        "iv_rank_1y": scan_meta.get("iv_rank_1y"),
                        "vrp_pct_252d": scan_meta.get("vrp_pct_252d"),
                        "terrain_regime": scan_meta.get("terrain_regime"),
                        "lens_flags": scan_meta.get("lens_flags"),
                        "trade_date": (
                            scan_meta["trade_date"].isoformat()
                            if hasattr(scan_meta.get("trade_date"), "isoformat")
                            else scan_meta.get("trade_date")
                        ),
                    }
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
                    "score": (scan_meta or {}).get("composite_score") if scan_meta else None,
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

        # 6. Optional policy_suggestion draft (Y.3 A1) -----------------------
        policy_suggestion_draft_id: str | None = None
        suggestion_diff = policy_suggestion_from_plan(plan, policy)
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
                    "current_policy": policy,
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
