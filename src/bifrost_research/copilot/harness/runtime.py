"""Harness runtime — Wave A + Y + LS-2 Stock-first universe."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from bifrost_research.db.conn import rollback_quietly
from bifrost_research.copilot.harness.evidence import build_candidate_evidence
from bifrost_research.copilot.harness.plan_llm import (
    OP_ANALYZE_SYMBOL,
    OP_COMPOSE_REPORT,
    OP_PERSONA_EVALUATE,
    OP_RUN_BACKTEST,
)
from bifrost_research.copilot.harness import data_sources as ds
from bifrost_research.copilot.harness.gate import (
    apply_hit_rate_gate,
    lenses_from_flag_filter,
)
from bifrost_research.copilot.harness.planning import (
    _plan_for_objective,
    _playbook_rules_for,
)
from bifrost_research.copilot.harness.planning import (
    _heuristic_plan as _heuristic_plan,  # re-export: api/harness.py fast-create path
)
from bifrost_research.copilot.harness.policy_schema import parse_policy
from bifrost_research.copilot.harness.suggestion import (
    policy_suggestion_from_outcomes,
    policy_suggestion_from_plan,
)
from bifrost_research.copilot.harness.universe.registry import resolve_universe
from bifrost_research.copilot.harness.universe.types import FunnelStep
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


def _flush_live_trace(
    conn: _Connection,
    run_id: str,
    trace: list[dict[str, Any]],
    *,
    step: str,
    label: str,
    detail: str = "",
) -> None:
    """Persist mid-run trace so Pipeline can poll progress while status=running."""
    try:
        obj_repo.patch_run_trace(
            conn,
            run_id,
            {
                "events": list(trace),
                "progress": {"step": step, "label": label, "detail": detail},
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("mid-run trace flush failed for %s: %s", run_id, exc)
        rollback_quietly(conn)


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


def run_objective(
    conn: _Connection,
    *,
    objective: dict[str, Any],
    existing_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a harness run: universe → decay (legacy) → propose.

    When ``existing_run`` is provided (HTTP batch-run async start), skip
    ``create_run`` and continue that row so Pipeline can poll immediately.
    """
    if existing_run is not None:
        run = existing_run
        plan = existing_run.get("plan_json") if isinstance(existing_run.get("plan_json"), dict) else {}
        if not plan:
            plan = _plan_for_objective(objective, conn)
        run_id = str(run["id"])
    else:
        plan = _plan_for_objective(objective, conn)
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

        # The plan finally decides something. Until now run_objective ran a fixed
        # sequence and the plan was narration written into the trace and never
        # read — so an LLM "planner" could not change what happened. Optional
        # stages are honoured from the plan; propose_candidates and
        # await_approval stay outside its reach.
        plan_ops = {
            str(step.get("op"))
            for step in (plan.get("steps") or [])
            if isinstance(step, dict)
        }
        want_evidence = OP_ANALYZE_SYMBOL in plan_ops
        want_backtest = OP_RUN_BACKTEST in plan_ops
        # Persona eval + report default on for heuristic plans; LLM plans may omit.
        want_persona = OP_PERSONA_EVALUATE in plan_ops or (
            plan.get("generated_by") == "heuristic" and loop_policy.persona_evaluate
        )
        if not loop_policy.persona_evaluate:
            want_persona = False
        want_report = OP_COMPOSE_REPORT in plan_ops or plan.get("generated_by") == "heuristic"
        trace.append(
            {
                "step": "plan_ops",
                "ops": sorted(plan_ops),
                "evidence_enabled": want_evidence,
                "persona_evaluate": want_persona,
                "compose_report": want_report,
                "decision": f"persona={want_persona} report={want_report}",
            }
        )
        # Enrich plan event with a short decision for live UI.
        if trace and trace[0].get("step") == "plan":
            trace[0]["decision"] = f"generated_by={plan.get('generated_by')}"
            trace[0]["label"] = "Plan"
        _flush_live_trace(
            conn,
            run_id,
            trace,
            step="plan",
            label="Plan",
            detail=str(plan.get("generated_by") or "heuristic"),
        )

        # 1. Resolve universe --------------------------------------------------
        # Fetch a wider set so discovery_assist can veto/boost before max_candidates.
        fetch_n = max(max_n * 3, max_n)
        universe = resolve_universe(conn, loop_policy, limit=fetch_n)
        universe_symbols = list(universe.symbols)
        row_meta_by_symbol = dict(universe.row_meta_by_symbol)

        # Wave 3 — Discover assist at funnel exit (does not replace Policy).
        from bifrost_research.copilot.harness.discovery_assist import apply_discovery_assist

        discovery_rules = _playbook_rules_for(
            conn,
            {
                **objective,
                "persona": "discovery",
            },
        )
        assist = apply_discovery_assist(
            universe_symbols,
            policy=policy_raw,
            playbook_rules=discovery_rules,
            row_meta_by_symbol=row_meta_by_symbol,
        )
        assisted_symbols = list(assist.get("symbols") or universe_symbols)
        funnel_dicts = universe.funnel_dicts()
        if assist.get("funnel_step"):
            funnel_dicts = list(funnel_dicts) + [assist["funnel_step"]]

        # The last cut, and the one nothing showed. The resolver was asked for
        # `max_n * 3` so discovery_assist had room to veto, then this line took
        # the top `max_n` — a drop of 24 -> 8 that never reached the funnel, so
        # the console headlined the 24 as if they had been proposed. Everything
        # downstream (candidates, personas, the draft) works off `max_n`.
        universe_symbols = assisted_symbols[:max_n]
        if len(assisted_symbols) != len(universe_symbols):
            funnel_dicts = list(funnel_dicts) + [
                FunnelStep(
                    name="max_candidates",
                    in_count=len(assisted_symbols),
                    out_count=len(universe_symbols),
                    filter_summary=f"policy.max_candidates = {max_n}",
                ).to_dict()
            ]

        veto_n = len(assist.get("veto") or [])
        boost_n = len(assist.get("boost") or [])
        scan_decision = (
            f"returned={len(universe_symbols)} veto={veto_n} boost={boost_n}"
        )
        trace.append(
            {
                "step": "scan_universe",
                "label": "Scan universe",
                "returned": len(universe_symbols),
                "symbols": universe_symbols,
                "universe_mode": universe.universe_mode,
                "funnel": funnel_dicts,
                "layer_results": universe.layer_results,
                "option_overlay_applied": universe.option_overlay_applied,
                "policy_warnings": universe.policy_warnings,
                "discovery_assist": {
                    "enabled": assist.get("enabled"),
                    "boost": assist.get("boost"),
                    "veto": assist.get("veto"),
                    "notes": assist.get("notes"),
                },
                "flag_filter": flag_filter if loop_policy.universe_mode == "scan_legacy" else None,
                "min_composite_score": loop_policy.min_composite_score,
                "preset": loop_policy.preset,
                "decision": scan_decision,
            }
        )
        _flush_live_trace(
            conn,
            run_id,
            trace,
            step="scan_universe",
            label="Scan universe",
            detail=scan_decision,
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
                trace_json={
                    "events": trace,
                    "progress": {
                        "step": "no_data",
                        "label": "No data",
                        "detail": "universe empty",
                    },
                },
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
                # The score reached the draft payload but never the pool row, so
                # Candidate Pool showed an em dash for every harness candidate
                # while the Inbox card for the same symbol showed 82.8.
                score=_primary_score(sym_meta),
                lens_snapshot=lens_snapshot,
                tags=["harness", data_source],
                source_ref={"objective_id": objective["id"], "run_id": run_id},
            )
            candidate_ids.append(row["id"])
            item: dict[str, Any] = {
                "id": row["id"],
                "symbol": row["symbol"],
                "score": _primary_score(sym_meta),
            }
            # A score with no reasoning is an opinion. Attach why it was picked,
            # where the price sits, whether this source has ever been right, and
            # what would make the call wrong — so the Inbox card can be judged
            # instead of merely approved.
            if want_evidence:
                # Evidence enriches a proposal; it is not a precondition for
                # making one. A failure here must not cost the Owner the whole
                # batch — the card just says the chain could not be built.
                try:
                    item["evidence"] = build_candidate_evidence(
                        conn,
                        row["symbol"],
                        source=source_label,
                        min_score=loop_policy.layers.sepa.min_score
                        if loop_policy.is_stock_mode()
                        else loop_policy.min_composite_score,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("evidence build failed for %s: %s", row["symbol"], exc)
                    rollback_quietly(conn)
                    item["evidence"] = {
                        "symbol": row["symbol"],
                        "status": "not_measured",
                        "reason": f"evidence build failed: {exc}",
                    }
            proposed_items.append(item)
            trace.append({"step": "propose_candidate", "symbol": row["symbol"], "id": row["id"]})

        propose_decision = f"proposed={len(proposed_items)}"
        trace.append(
            {
                "step": "propose_candidates",
                "label": "Propose candidates",
                "count": len(proposed_items),
                "symbols": [i.get("symbol") for i in proposed_items],
                "decision": propose_decision,
            }
        )
        _flush_live_trace(
            conn,
            run_id,
            trace,
            step="propose_candidates",
            label="Propose candidates",
            detail=propose_decision,
        )

        # 4b. Persona eval (Wave 1) — before candidate_batch draft --------------
        persona_eval_summary: dict[str, Any] | None = None
        if want_persona and proposed_items:
            try:
                from bifrost_research.copilot.harness.persona_eval import evaluate_candidates

                persona_eval_summary = evaluate_candidates(
                    proposed_items,
                    policy={
                        **policy_raw,
                        "require_validate_pass": loop_policy.require_validate_pass,
                    },
                    owner_id=str(objective.get("owner_id") or "owner"),
                )
                blocked = int(persona_eval_summary.get("blocked_by_validate") or 0)
                eligible = persona_eval_summary.get("auto_approve_eligible")
                persona_decision = (
                    f"mode={persona_eval_summary.get('mode')} "
                    f"blocked_by_validate={blocked} "
                    f"auto_approve_eligible={eligible}"
                )
                trace.append(
                    {
                        "step": "persona_evaluate",
                        "label": "Persona eval",
                        "decision": persona_decision,
                        **{
                            k: persona_eval_summary[k]
                            for k in (
                                "status",
                                "mode",
                                "fallback_used",
                                "fallback_count",
                                "holdings_status",
                                "holdings_count",
                                "symbols_evaluated",
                                "blocked_by_validate",
                                "auto_approve_eligible",
                                "eligible_count",
                                "per_symbol",
                            )
                            if k in persona_eval_summary
                        },
                    }
                )
                _flush_live_trace(
                    conn,
                    run_id,
                    trace,
                    step="persona_evaluate",
                    label="Persona eval",
                    detail=persona_decision,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("persona_evaluate failed for run %s: %s", run_id, exc)
                rollback_quietly(conn)
                persona_eval_summary = {"status": "error", "error": str(exc)[:200]}
                trace.append(
                    {
                        "step": "persona_evaluate",
                        "error": str(exc)[:200],
                        "decision": "error",
                    }
                )
                _flush_live_trace(
                    conn,
                    run_id,
                    trace,
                    step="persona_evaluate",
                    label="Persona eval",
                    detail=str(exc)[:120],
                )

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
        # Has this signal ever worked? The plan asks for the check; a failure to
        # answer must not cost the Owner the batch, so it is recorded as
        # not_measured rather than swallowed or raised.
        backtest_summary: dict[str, Any] | None = None
        if want_backtest:
            try:
                from bifrost_research.engines.backtest.event_defs import EventDef
                from bifrost_research.engines.backtest.event_query import run_event_query

                bt = run_event_query(
                    EventDef(kind="earnings", params={}),
                    "long_stock_event",
                    lookback_years=3,
                    conn=conn,
                )
                backtest_summary = {
                    "status": "ok",
                    "template": "long_stock_event",
                    "summary": bt.get("summary") or {},
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("run_backtest failed for run %s: %s", run_id, exc)
                rollback_quietly(conn)
                backtest_summary = {"status": "not_measured", "reason": str(exc)[:200]}
            trace.append({"step": "run_backtest", "result": backtest_summary})

        candidate_payload: dict[str, Any] = {
            "objective_id": objective["id"],
            "run_id": run_id,
            "items": proposed_items,
            "title": objective.get("title"),
            "description": objective.get("description"),
            "data_source": data_source,
            "universe_mode": loop_policy.universe_mode,
            "funnel": funnel_dicts,
            "signal_decay": decay_summary,
            "hit_rate_gate": gate,
        }
        if backtest_summary is not None:
            candidate_payload["backtest"] = backtest_summary
        if gate.get("applied") and not gate.get("ok"):
            candidate_payload["hit_rate_warn"] = True
        if persona_eval_summary:
            candidate_payload["persona_eval"] = {
                "mode": persona_eval_summary.get("mode"),
                "fallback_used": persona_eval_summary.get("fallback_used"),
                "fallback_count": persona_eval_summary.get("fallback_count"),
                "holdings_status": persona_eval_summary.get("holdings_status"),
                "blocked_by_validate": persona_eval_summary.get("blocked_by_validate"),
                "auto_approve_eligible": persona_eval_summary.get("auto_approve_eligible"),
            }
            candidate_payload["auto_approve_eligible"] = bool(
                persona_eval_summary.get("auto_approve_eligible")
            )
            if any(
                i.get("blocked_by_validate") or i.get("net_stance") == "oppose"
                for i in proposed_items
            ):
                candidate_payload["persona_dissent"] = True

        # The report is the Loop's actual deliverable: why each name, where its
        # price sits, how this source has settled, and what would unmake the call.
        if want_report:
            try:
                from bifrost_research.copilot.harness.report import compose_report

                candidate_payload["report"] = compose_report(
                    objective=objective,
                    run_id=run_id,
                    items=proposed_items,
                    funnel=funnel_dicts,
                    backtest=backtest_summary,
                )
                report_decision = (
                    f"candidates={len(proposed_items)} "
                    f"settled={candidate_payload['report']['coverage']['with_settled_record']}"
                )
                trace.append(
                    {
                        "step": "compose_report",
                        "label": "Compose report",
                        "candidates": len(proposed_items),
                        "with_settled_record": candidate_payload["report"]["coverage"][
                            "with_settled_record"
                        ],
                        "net_stance_counts": candidate_payload["report"]
                        .get("coverage", {})
                        .get("net_stance_counts"),
                        "decision": report_decision,
                    }
                )
                _flush_live_trace(
                    conn,
                    run_id,
                    trace,
                    step="compose_report",
                    label="Compose report",
                    detail=report_decision,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("compose_report failed for run %s: %s", run_id, exc)
                trace.append(
                    {
                        "step": "compose_report",
                        "error": str(exc)[:200],
                        "decision": "error",
                    }
                )
        draft = draft_repo.insert_draft(
            conn,
            kind="candidate_batch",
            payload=candidate_payload,
            scope=f"objective:{objective['id']}",
            generated_by="harness",
            linked_action_id=action["id"],
        )
        draft_ids.append(draft["id"])
        draft_decision = (
            f"draft={draft['id']} "
            f"auto_approve_eligible="
            f"{bool((persona_eval_summary or {}).get('auto_approve_eligible', True))}"
        )
        trace.append(
            {
                "step": "draft_candidate_batch",
                "label": "Draft candidate batch",
                "draft_id": draft["id"],
                "decision": draft_decision,
            }
        )
        _flush_live_trace(
            conn,
            run_id,
            trace,
            step="draft_candidate_batch",
            label="Draft candidate batch",
            detail=draft_decision,
        )

        policy_suggestion_draft_id: str | None = None
        suggestion_diff = policy_suggestion_from_plan(plan, policy_raw)
        suggestion_source = "harness_llm_plan"
        suggestion_reasoning = plan.get("llm_reasoning")
        if not suggestion_diff and persona_eval_summary:
            outcome_summary: dict[str, Any] | None = None
            try:
                from bifrost_research.api.candidate_outcome import build_summary

                outcome_summary = build_summary(conn, days=90)
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "candidate_outcome summary skipped for policy suggestion: %s",
                    str(exc)[:160],
                )
                rollback_quietly(conn)
            outcome_sug = policy_suggestion_from_outcomes(
                persona_eval_summary,
                current_policy=policy_raw,
                outcome_summary=outcome_summary,
            )
            if outcome_sug:
                suggestion_diff = outcome_sug.get("suggestion")
                suggestion_source = "persona_eval_outcomes"
                suggestion_reasoning = outcome_sug.get("reasoning")
        if suggestion_diff:
            ps_action = action_repo.insert_action(
                conn,
                action_kind="harness_policy_suggestion",
                action_source="harness",
                input_payload={
                    "objective_id": objective["id"],
                    "run_id": run_id,
                    "source_plan_generated_by": plan.get("generated_by"),
                    "suggestion_source": suggestion_source,
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
                    "source": suggestion_source,
                    "llm_model": plan.get("llm_model"),
                    "llm_reasoning": suggestion_reasoning,
                    "evidence": (
                        {
                            "persona_eval": {
                                "blocked_by_validate": (
                                    persona_eval_summary or {}
                                ).get("blocked_by_validate"),
                                "symbols_evaluated": (
                                    persona_eval_summary or {}
                                ).get("symbols_evaluated"),
                            }
                        }
                        if suggestion_source == "persona_eval_outcomes"
                        else None
                    ),
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
                    "source": suggestion_source,
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
            "persona_eval": persona_eval_summary,
            "auto_approve_eligible": bool(
                (persona_eval_summary or {}).get("auto_approve_eligible", True)
            ),
        }
        finished = obj_repo.finish_run(
            conn,
            run_id,
            status="awaiting_approval",
            trace_json={
                "events": trace,
                "progress": {
                    "step": "awaiting_approval",
                    "label": "Awaiting approval",
                    "detail": f"candidates={len(candidate_ids)} drafts={len(draft_ids)}",
                },
            },
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
            trace_json={
                "events": trace,
                "error": str(exc),
                "progress": {
                    "step": "failed",
                    "label": "Failed",
                    "detail": str(exc)[:160],
                },
            },
            outputs={"candidate_ids": candidate_ids, "draft_ids": draft_ids},
        )
        raise
