"""Per-candidate Persona evaluation chain — Policy × Personas E2E Wave 1.

Order (fixed): analyze → portfolio → validate → verdict.

Default path is **deterministic heuristics** from SQL evidence
(``persona_heuristic``) so CI / offline runs stay reproducible. Optional
headless LLM agents activate when ``BIFROST_PERSONA_EVAL_AGENTS=1``: B2 of
research-loop-automation judges every candidate with every model in
``PERSONA_EVAL_MODELS`` and keeps only what they agree on (``persona_judge``).
This module is the entry point that runs the batch and shapes the trace.

D10 BLOCKED — advisory stances only; never places orders.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from bifrost_research.copilot import rate_limit
from bifrost_research.copilot.harness.persona_heuristic import (
    EVAL_AGENTS,
    HOLDINGS_SNAPSHOT_TIMEOUT_S,
    HOLDINGS_UNAVAILABLE_TTL_S,
    STANCES,
    heuristic_verdicts_for_item,
    load_held_symbols,
    net_stance_from_verdicts,
    reset_holdings_probe_cache,
    validate_stance,
)
from bifrost_research.copilot.harness.persona_judge import (
    DEFAULT_EVAL_MODELS,
    DISSENT,
    SPEND_ACTION_KIND,
    _fallback_rows,
    _group_rows_by_model,
    _judge_symbol,
    _new_call,
    _persist_spend,
    _seed_spend_from_ledger,
    consensus,
    eval_models,
    judge_max_turns,
    most_severe,
    provider_of,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = float(os.environ.get("BIFROST_PERSONA_EVAL_TIMEOUT_S", "90"))

# What the run trace keeps from the summary, in this order.
TRACE_KEYS = (
    "status",
    "mode",
    "models",
    "agreement",
    "dissent_count",
    "budget_s",
    "budget_exhausted_symbols",
    "judge_max_turns",
    "spend_rows_written",
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

# What the Inbox card needs to say who judged, what it cost, and whether they agreed.
_INBOX_MODEL_KEYS = (
    "model",
    "provider",
    "calls",
    "ok",
    "fallback",
    "cap_exceeded",
    "elapsed_ms",
    "cost_usd",
    "cap_usd",
    "spent_today_usd",
)


def inbox_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """The compact ``persona_eval`` block a candidate_batch draft carries."""
    return {
        "mode": summary.get("mode"),
        "models": [
            {k: m.get(k) for k in _INBOX_MODEL_KEYS}
            for m in (summary.get("models") or [])
            if isinstance(m, dict)
        ],
        "agreement": summary.get("agreement"),
        "dissent_count": summary.get("dissent_count"),
        "fallback_used": summary.get("fallback_used"),
        "fallback_count": summary.get("fallback_count"),
        "holdings_status": summary.get("holdings_status"),
        "blocked_by_validate": summary.get("blocked_by_validate"),
        "auto_approve_eligible": summary.get("auto_approve_eligible"),
    }


def eval_budget_s() -> float:
    """Whole-batch wall-clock budget for the judge stage.

    Read at call time, not import time, so the Cron's env and a test's
    monkeypatch both take effect. Symbols left when it runs out are scored by
    the heuristic and marked as such — the run finishes, and the trace says
    which names never got a real judge.
    """
    raw = os.environ.get("BIFROST_PERSONA_EVAL_TIMEOUT_S", "").strip()
    try:
        return float(raw) if raw else DEFAULT_TIMEOUT_S
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def agents_enabled() -> bool:
    if _env_flag("BIFROST_PERSONA_EVAL_SKIP_AGENT"):
        return False
    return _env_flag("BIFROST_PERSONA_EVAL_AGENTS")


def evaluate_candidates(
    items: list[dict[str, Any]],
    *,
    policy: dict[str, Any] | None = None,
    owner_id: str = "owner",
    model_id: str | None = None,
    conn: Any | None = None,
    run_id: str | None = None,
    objective_id: str | None = None,
) -> dict[str, Any]:
    """Attach ``agent_verdicts`` (+ flags) onto each item; return trace summary.

    Agent mode judges every item with every model in ``eval_models()`` and
    reduces the opinions with ``consensus``. ``conn`` is optional: with it the
    per-provider caps are seeded from, and this run's spend written to,
    ``research.ai_action_log``; without it the caps are process-local.
    """
    policy = policy or {}
    require_validate_pass = policy.get("require_validate_pass", True)
    if isinstance(require_validate_pass, str):
        require_validate_pass = require_validate_pass.strip().lower() in ("1", "true", "yes")

    use_agents = agents_enabled()
    models = eval_models(model_id) if use_agents else []
    mcp_url = os.environ.get("RESEARCH_MCP_SSE_URL", "")
    held_symbols, holdings_status = load_held_symbols()

    spent_before: dict[str, float] = {}
    if use_agents and conn is not None:
        spent_before = _seed_spend_from_ledger(conn, {provider_of(m) for m in models})

    budget_s = eval_budget_s()
    started = time.perf_counter()
    per_symbol: list[dict[str, Any]] = []
    blocked = 0
    fallback_count = 0
    budget_exhausted = 0
    agreement_counts: dict[str, int] = {"agree": 0, DISSENT: 0, "single": 0}
    model_totals: dict[str, dict[str, Any]] = {
        m: {
            "model": m,
            "provider": provider_of(m),
            "calls": 0,
            "ok": 0,
            "fallback": 0,
            "cap_exceeded": 0,
            "elapsed_ms": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
        }
        for m in models
    }

    for item in items:
        calls: list[dict[str, Any]] = []
        if use_agents:
            elapsed = time.perf_counter() - started
            if elapsed > budget_s:
                budget_exhausted += 1
                error = f"eval budget exhausted ({budget_s:.0f}s)"
                verdicts = []
                for m in models:
                    call = _new_call(m)
                    call["fallback"] = True
                    call["error"] = error
                    calls.append(call)
                    verdicts.extend(
                        _fallback_rows(
                            item,
                            model=m,
                            error=error,
                            held_symbols=held_symbols,
                            holdings_status=holdings_status,
                        )
                    )
            else:
                verdicts, calls = _judge_symbol(
                    item,
                    models=models,
                    owner_id=owner_id,
                    mcp_url=mcp_url,
                    held_symbols=held_symbols,
                    holdings_status=holdings_status,
                )
            for call in calls:
                t = model_totals[call["model"]]
                t["calls"] += 1
                t["ok"] += int(bool(call["ok"]))
                t["fallback"] += int(bool(call["fallback"]))
                t["cap_exceeded"] += int(bool(call["cap_exceeded"]))
                t["elapsed_ms"] += int(call["elapsed_ms"] or 0)
                t["input_tokens"] += int(call["input_tokens"] or 0)
                t["output_tokens"] += int(call["output_tokens"] or 0)
                t["cost_usd"] = round(t["cost_usd"] + float(call["cost_usd"] or 0.0), 6)
            fallback_models = {c["model"] for c in calls if c["fallback"]}
            verdict = consensus(
                _group_rows_by_model(verdicts, models), fallback_models=fallback_models
            )
        else:
            verdicts = heuristic_verdicts_for_item(
                item,
                held_symbols=held_symbols,
                holdings_status=holdings_status,
            )
            verdict = {
                "net_stance": net_stance_from_verdicts(verdicts),
                "validate_stance": validate_stance(verdicts),
                "agreement": "single",
                "by_model": {},
            }

        if any(v.get("source") == "heuristic_fallback" for v in verdicts):
            fallback_count += 1
        agreement_counts[verdict["agreement"]] = agreement_counts.get(verdict["agreement"], 0) + 1

        net = verdict["net_stance"]
        v_stance = verdict["validate_stance"]
        blocked_by_validate = bool(require_validate_pass and v_stance == "oppose")
        if blocked_by_validate:
            blocked += 1

        ev = item.get("evidence")
        if not isinstance(ev, dict):
            ev = {}
            item["evidence"] = ev
        ev["agent_verdicts"] = verdicts
        ev["net_stance"] = net
        ev["agreement"] = verdict["agreement"]
        item["blocked_by_validate"] = blocked_by_validate
        item["net_stance"] = net
        item["agreement"] = verdict["agreement"]

        per_symbol.append(
            {
                "symbol": item.get("symbol"),
                "net_stance": net,
                "validate_stance": v_stance,
                "blocked_by_validate": blocked_by_validate,
                "agreement": verdict["agreement"],
                "models": [
                    {
                        "model": c["model"],
                        "provider": c["provider"],
                        "net": verdict["by_model"].get(c["model"], {}).get("net"),
                        "validate": verdict["by_model"].get(c["model"], {}).get("validate"),
                        "ok": c["ok"],
                        "fallback": c["fallback"],
                        "cap_exceeded": c["cap_exceeded"],
                        "elapsed_ms": c["elapsed_ms"],
                        "cost_usd": c["cost_usd"],
                        "error": c["error"],
                    }
                    for c in calls
                ],
                "verdicts": verdicts,
            }
        )

    eligible = [
        i
        for i in items
        if not i.get("blocked_by_validate") and i.get("net_stance") in {"support", "caution"}
    ]
    dissent_count = sum(1 for i in items if i.get("net_stance") == DISSENT)
    # Agreement is the gate: every judge on every symbol on the same side of
    # support / caution, nobody blocked, nobody fell back.
    auto_approve_eligible = (
        len(items) > 0
        and blocked == 0
        and dissent_count == 0
        and all((i.get("net_stance") in {"support", "caution"}) for i in items)
    )

    model_summaries = list(model_totals.values())
    for ms in model_summaries:
        usage = rate_limit.provider_usage(ms["provider"])
        ms["cap_usd"] = usage.cap_usd
        ms["spent_today_usd"] = usage.cost_today_usd
        ms["spent_before_run_usd"] = round(float(spent_before.get(ms["provider"], 0.0)), 6)

    spend_rows_written = 0
    if use_agents and conn is not None:
        spend_rows_written = _persist_spend(
            conn, model_summaries=model_summaries, run_id=run_id, objective_id=objective_id
        )

    mode = "agent" if use_agents else "heuristic"
    return {
        "status": "completed",
        "mode": mode,
        "models": model_summaries,
        "agreement": agreement_counts,
        "dissent_count": dissent_count,
        "budget_s": budget_s,
        "budget_exhausted_symbols": budget_exhausted,
        "judge_max_turns": judge_max_turns() if use_agents else None,
        "spend_rows_written": spend_rows_written,
        "fallback_used": bool(use_agents and fallback_count > 0),
        "fallback_count": fallback_count,
        "holdings_status": holdings_status,
        "holdings_count": len(held_symbols) if held_symbols is not None else None,
        "require_validate_pass": require_validate_pass,
        "symbols_evaluated": len(items),
        "blocked_by_validate": blocked,
        "auto_approve_eligible": auto_approve_eligible,
        "eligible_count": len(eligible),
        "per_symbol": per_symbol,
    }


__all__ = [
    "DEFAULT_EVAL_MODELS",
    "DISSENT",
    "EVAL_AGENTS",
    "HOLDINGS_SNAPSHOT_TIMEOUT_S",
    "HOLDINGS_UNAVAILABLE_TTL_S",
    "SPEND_ACTION_KIND",
    "STANCES",
    "TRACE_KEYS",
    "agents_enabled",
    "consensus",
    "eval_budget_s",
    "eval_models",
    "evaluate_candidates",
    "heuristic_verdicts_for_item",
    "inbox_summary",
    "load_held_symbols",
    "most_severe",
    "net_stance_from_verdicts",
    "provider_of",
    "reset_holdings_probe_cache",
    "validate_stance",
]
