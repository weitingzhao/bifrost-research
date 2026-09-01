"""CuratorRun user prompt assembly — Wave LO-1 / LO-2 / LS-4."""

from __future__ import annotations

import json
from typing import Any


def _funnel_summary(trace: dict[str, Any]) -> str:
    events = trace.get("events") if isinstance(trace, dict) else []
    if not isinstance(events, list):
        return "(no funnel)"
    for ev in events:
        if isinstance(ev, dict) and ev.get("step") == "scan_universe":
            funnel = ev.get("funnel")
            if isinstance(funnel, list) and funnel:
                parts = []
                for step in funnel:
                    if not isinstance(step, dict):
                        continue
                    parts.append(
                        f"{step.get('name')}: {step.get('in_count')}→{step.get('out_count')} "
                        f"({step.get('filter', '')})"
                    )
                return "; ".join(parts) if parts else "(empty funnel)"
            mode = ev.get("universe_mode", "unknown")
            syms = ev.get("symbols") or []
            return f"mode={mode}, symbols={len(syms)}"
    return "(no scan_universe event)"


def build_curator_prompt(
    *,
    run: dict[str, Any],
    objective: dict[str, Any],
    batch_pass: str,
) -> str:
    outputs = run.get("outputs") or {}
    trace = run.get("trace_json") or {}
    events = trace.get("events") if isinstance(trace, dict) else []
    scan_symbols: list[str] = []
    if isinstance(events, list):
        for ev in events:
            if isinstance(ev, dict) and ev.get("step") == "scan_universe":
                syms = ev.get("symbols")
                if isinstance(syms, list):
                    scan_symbols = [str(s).upper() for s in syms if s]

    candidate_ids = outputs.get("candidate_ids") or []
    draft_ids = outputs.get("draft_ids") or []
    policy = objective.get("policy_json") or {}
    universe_mode = policy.get("universe_mode") or outputs.get("universe_mode") or "scan_legacy"
    funnel_line = _funnel_summary(trace if isinstance(trace, dict) else {})

    lines = [
        "Headless CuratorRun for Research Loop Stage 2 (batch execute mode).",
        f"Objective: {objective.get('title')} ({objective.get('id')})",
        f"Run: {run.get('id')} status={run.get('status')}",
        f"Universe mode: {universe_mode}",
        f"Funnel: {funnel_line}",
        "",
        "Candidate symbols (prioritize these): " + (", ".join(scan_symbols) or "(none)"),
        f"Candidate pool rows: {len(candidate_ids)}",
        f"Pending draft ids from harness: {len(draft_ids)}",
        "",
        "Stock-first review checklist (when universe_mode is stock_composite/sepa):",
        "1. Confirm SEPA stage/path/score justification per symbol.",
        "2. Note momentum grade/score when present in lens_snapshot.",
        "3. Note event_importance when events layer contributed.",
        "4. Option IV/VRP/GEX is optional — missing option fields are NOT rejection reasons.",
        "",
        "Tasks:",
        "1. For each symbol, read stock + optional vol context via MCP read tools.",
        "2. promote_to_hypothesis for strong candidates (dry_run=false).",
        "3. propose_order_intent for at most one structure per symbol (advisory only).",
        "4. attach_backtest_evidence when backtest data exists.",
        "5. draft_decision or eod_verdict drafts where appropriate.",
        "",
        "Batch execute: all write tools MUST use dry_run=false with this approval_token:",
        batch_pass,
        "",
        "Policy snapshot:",
        json.dumps(policy, indent=2, default=str),
        "",
        "D10 BLOCKED — never place live orders.",
    ]
    return "\n".join(lines)


__all__ = ["build_curator_prompt"]
