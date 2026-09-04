"""Plan construction for the harness — extracted from runtime (P-UI Wave).

`run_objective` had grown past the 800-line ratchet with the persona, discovery
and report stages landing inline. Planning is the part that stands on its own:
it decides *what the run will do* and touches nothing the run does afterwards.
Same split gate.py and suggestion.py got out of this module in 0.48.3.

runtime re-exports these names, so `runtime._heuristic_plan` stays a valid patch
target for api/harness.py's fast-create path and its test.
"""

from __future__ import annotations

import logging
from typing import Any

from bifrost_research.db.conn import rollback_quietly
from bifrost_research.copilot.harness import plan_llm
from bifrost_research.copilot.harness.plan_llm import (
    OP_ANALYZE_SYMBOL,
    OP_COMPOSE_REPORT,
    OP_PERSONA_EVALUATE,
)
from bifrost_research.copilot.harness.policy_schema import LoopPolicy, parse_policy

logger = logging.getLogger(__name__)


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
                "op": OP_ANALYZE_SYMBOL,
                "note": (
                    "Attach selection rationale, price context, option analytics "
                    "(absent for most symbols) and this source's settled hit rate"
                ),
            },
            {
                "op": "propose_candidates",
                "max": max_candidates,
                "note": "Propose top candidates into pool + draft (universe first; fallback seed_symbols)",
            },
            {
                "op": OP_PERSONA_EVALUATE,
                "note": (
                    "Headless Personas: analyze → portfolio → validate → verdict "
                    "(heuristic by default; set BIFROST_PERSONA_EVAL_AGENTS=1 for LLM)"
                ),
            },
            {
                "op": OP_COMPOSE_REPORT,
                "note": "Compose per-symbol why / risks / falsify / net_stance for Inbox",
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


def _playbook_rules_for(conn: Any, objective: dict[str, Any]) -> list[dict[str, Any]]:
    """The Owner's rules for the persona running this objective.

    Policy decides which symbols the Loop looks at; these decide how it judges
    them — the half of "brain + strategy" the Owner writes. Fail-soft on purpose:
    a Playbook that cannot be read must not cost the run its plan, it just plans
    without the rules, exactly as before they existed.
    """
    try:
        from bifrost_research.repositories import playbook as playbook_repo

        return playbook_repo.list_rules_for_agent(
            conn,
            owner_id=str(objective.get("owner_id") or "owner"),
            agent_name=str(objective.get("persona") or "loop_curator"),
            limit=20,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("playbook rules unavailable for plan: %s", exc)
        rollback_quietly(conn)
        return []


def _plan_for_objective(
    objective: dict[str, Any],
    conn: Any | None = None,
) -> dict[str, Any]:
    heuristic = _heuristic_plan(objective)
    policy = objective.get("policy_json") or {}
    enabled, reason = plan_llm.is_llm_plan_enabled(policy)
    if not enabled:
        heuristic["fallback_reason"] = f"llm_disabled: {reason}"
        return heuristic

    rules = _playbook_rules_for(conn, objective) if conn is not None else []
    llm_result = plan_llm.generate_plan_llm(objective, playbook_rules=rules)
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
        # Visible in the trace: a plan made under rules is a different plan.
        "playbook_rules_applied": len(rules),
    }
