"""A harness run, digested for a reader — research-loop-automation D1.

``digest_run`` folds the run row, its trace and the candidate_batch draft into
one object a Copilot agent (or a person) can answer questions from: what the
plan decided and where it came from, how the funnel cut, what each judge said
about each name and whether they agreed, the evidence behind every candidate,
and the report's why / price / settled / wrong_if. ``explain_candidate`` is
the same for one symbol, plus the candidate row, its hypothesis and any
settled validation.

Nothing here writes, and nothing here re-derives: every block quotes the run's
own record, so an explanation can be checked against the Inbox card it
describes. A block the run never produced stays absent instead of being
filled from live data.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import backtest_run as bt_repo
from bifrost_research.repositories import candidate_pool as cand_repo
from bifrost_research.repositories import hypothesis as hyp_repo
from bifrost_research.repositories import objective as obj_repo

ADVISORY = "D10 BLOCKED — a research proposal and its evidence; nothing here is an order."

VERDICT_KEYS = ("agent", "stance", "summary", "confidence", "source", "model", "fallback", "risks", "falsify")
EVIDENCE_KEYS = (
    "selection",
    "price_context",
    "option_analytics",
    "track_record",
    "invalidation",
    "net_stance",
    "agreement",
)
STEP_KEYS = ("step", "label", "decision", "at_ms", "error")
PLAN_KEYS = ("generated_by", "llm_model", "llm_provider", "llm_attempts", "fallback_reason", "llm_reasoning")
OUTPUT_KEYS = (
    "candidate_ids",
    "draft_ids",
    "policy_suggestion_draft_id",
    "data_source",
    "universe_mode",
    "auto_approve_eligible",
    "approve_all",
    "approve_skipped",
    "trust",
    "curator",
)
HYPOTHESIS_KEYS = ("id", "title", "status", "resolution_json", "linked_backtest_ids", "created_at")
CANDIDATE_ROW_KEYS = ("status", "source", "source_ref", "tags", "lens_snapshot", "trade_date", "hypothesis_id")
MAX_VALIDATION_ROWS = 3


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _events(run: dict[str, Any]) -> list[dict[str, Any]]:
    events = _dict(run.get("trace_json")).get("events")
    return [e for e in (events or []) if isinstance(e, dict)]


def _drafts_for_run(conn: Any, run: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for draft_id in _dict(run.get("outputs")).get("draft_ids") or []:
        draft = draft_repo.get_draft(conn, str(draft_id))
        if draft:
            out.append(draft)
    return out


def _candidate_batch_payload(drafts: list[dict[str, Any]]) -> dict[str, Any]:
    for draft in drafts:
        if draft.get("kind") == "candidate_batch":
            return _dict(draft.get("payload"))
    return {}


def verdicts_of(item: dict[str, Any]) -> list[dict[str, Any]]:
    """The judges' stances on one candidate, one row per (model, agent)."""
    raw = _dict(item.get("evidence")).get("agent_verdicts")
    return [
        {k: v.get(k) for k in VERDICT_KEYS if k in v}
        for v in (raw or [])
        if isinstance(v, dict)
    ]


def candidate_view(item: dict[str, Any], sections: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One candidate as the digest carries it: stance, judges, evidence, report section."""
    evidence = _dict(item.get("evidence"))
    symbol = str(item.get("symbol") or "").upper()
    return {
        "id": item.get("id"),
        "symbol": symbol,
        "score": item.get("score"),
        "net_stance": item.get("net_stance") or evidence.get("net_stance"),
        "agreement": item.get("agreement") or evidence.get("agreement"),
        "blocked_by_validate": bool(item.get("blocked_by_validate")),
        "verdicts": verdicts_of(item),
        "evidence": {k: evidence.get(k) for k in EVIDENCE_KEYS if k in evidence},
        "report": sections.get(symbol),
    }


def digest_run(conn: Any, run_id: str) -> dict[str, Any] | None:
    """The whole run in one object, or None when there is no such run."""
    run = obj_repo.get_run(conn, run_id)
    if run is None:
        return None
    objective = obj_repo.get_objective(conn, str(run.get("objective_id") or "")) or {}
    drafts = _drafts_for_run(conn, run)
    payload = _candidate_batch_payload(drafts)
    outputs = _dict(run.get("outputs"))
    plan = _dict(run.get("plan_json"))
    trace = _dict(run.get("trace_json"))
    report = _dict(payload.get("report"))
    sections = {
        str(s.get("symbol") or "").upper(): s
        for s in (report.get("candidates") or [])
        if isinstance(s, dict)
    }
    items = [i for i in (payload.get("items") or []) if isinstance(i, dict)]
    return {
        "run": {
            "id": run.get("id"),
            "objective_id": run.get("objective_id"),
            "objective_title": objective.get("title"),
            "status": run.get("status"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "progress": trace.get("progress"),
        },
        "policy": _dict(objective.get("policy_json")),
        "plan": {
            **{k: plan.get(k) for k in PLAN_KEYS if k in plan},
            "steps": [s.get("op") for s in (plan.get("steps") or []) if isinstance(s, dict)],
        },
        "steps": [{k: e.get(k) for k in STEP_KEYS if k in e} for e in _events(run)],
        "funnel": payload.get("funnel") or [],
        "gate": payload.get("hit_rate_gate") or outputs.get("hit_rate_gate"),
        "signal_decay": payload.get("signal_decay"),
        "persona": payload.get("persona_eval") or outputs.get("persona_eval"),
        "candidates": [candidate_view(i, sections) for i in items],
        "report": (
            {"coverage": report.get("coverage"), "note": report.get("note"), "backtest": report.get("backtest")}
            if report
            else None
        ),
        "backtest": payload.get("backtest"),
        "outputs": {k: outputs.get(k) for k in OUTPUT_KEYS if k in outputs},
        "drafts": [{"id": d.get("id"), "kind": d.get("kind"), "status": d.get("status")} for d in drafts],
        "advisory": ADVISORY,
    }


def explain_candidate(conn: Any, run_id: str, symbol: str) -> dict[str, Any] | None:
    """One candidate of a run: why it was proposed and what would unmake it, from the run's own record."""
    digest = digest_run(conn, run_id)
    if digest is None:
        return None
    sym = (symbol or "").strip().upper()
    cand = next((c for c in digest["candidates"] if c["symbol"] == sym), None)
    if cand is None:
        return {
            "run": digest["run"],
            "symbol": sym,
            "found": False,
            "candidates": [c["symbol"] for c in digest["candidates"]],
            "advisory": ADVISORY,
        }
    row = cand_repo.get_candidate(conn, str(cand["id"])) if cand.get("id") else None
    hypothesis: dict[str, Any] | None = None
    validation: list[dict[str, Any]] = []
    hypothesis_id = (row or {}).get("hypothesis_id")
    if hypothesis_id:
        hyp = hyp_repo.get_hypothesis(conn, str(hypothesis_id))
        if hyp:
            hypothesis = {k: hyp.get(k) for k in HYPOTHESIS_KEYS if k in hyp}
            for backtest_id in (hyp.get("linked_backtest_ids") or [])[:MAX_VALIDATION_ROWS]:
                bt = bt_repo.get_run(conn, str(backtest_id))
                if bt:
                    validation.append(
                        {
                            "backtest_run_id": bt.get("id"),
                            "strategy_template": bt.get("strategy_template"),
                            "summary": bt.get("summary"),
                        }
                    )
    section = _dict(cand.get("report"))
    return {
        "run": digest["run"],
        "found": True,
        **cand,
        "candidate": {k: row.get(k) for k in CANDIDATE_ROW_KEYS if k in row} if row else None,
        "hypothesis": hypothesis,
        "validation": validation,
        "what_would_unmake_it": section.get("wrong_if") or _dict(cand.get("evidence")).get("invalidation") or [],
        "falsify": section.get("falsify") or [],
        "settled": section.get("settled"),
        "advisory": ADVISORY,
    }


def list_runs_digest(
    conn: Any,
    *,
    status: str | None = None,
    objective_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Recent runs, one line each — enough to pick the run to explain."""
    rows = obj_repo.list_runs(conn, status=status, objective_id=objective_id, limit=limit)
    titles: dict[str, str | None] = {}
    items: list[dict[str, Any]] = []
    for run in rows:
        oid = str(run.get("objective_id") or "")
        if oid not in titles:
            obj = obj_repo.get_objective(conn, oid) if oid else None
            titles[oid] = (obj or {}).get("title")
        outputs = _dict(run.get("outputs"))
        plan = _dict(run.get("plan_json"))
        progress = _dict(_dict(run.get("trace_json")).get("progress"))
        items.append(
            {
                "id": run.get("id"),
                "objective_id": oid,
                "objective_title": titles[oid],
                "status": run.get("status"),
                "started_at": run.get("started_at"),
                "finished_at": run.get("finished_at"),
                "candidates": len(outputs.get("candidate_ids") or []),
                "drafts": len(outputs.get("draft_ids") or []),
                "plan_generated_by": plan.get("generated_by"),
                "plan_model": plan.get("llm_model"),
                "data_source": outputs.get("data_source"),
                "universe_mode": outputs.get("universe_mode"),
                "progress": progress.get("detail"),
            }
        )
    return {"items": items, "count": len(items), "advisory": ADVISORY}
