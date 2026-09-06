"""Hypotheses resolve themselves — B3 of research-loop-automation (D-RLA-2).

A hypothesis born from a candidate batch carries its lineage in ``origin_ref``
(``candidate_id``, ``run_id``, ``objective_id``). Once the candidate's forward
window has settled in ``research.candidate_outcome``, the objective's
``policy_json.resolution`` rule decides:

    excess return over the benchmark at ``horizon_days`` sessions
        ≥ validate_excess → validated
        ≤ reject_excess   → rejected
        in between        → ambiguous: drafted for the Owner, as before

The two clear cases are applied directly, with the evidence written into
``hypothesis.resolution_json``, a row in ``research.ai_action_log`` and an
informational ``eod_verdict`` briefing. A hypothesis without lineage, or whose
window has not settled, is left to the existing review path.

D10 BLOCKED — a resolved hypothesis is a finding, never an order.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime
from typing import Any, Protocol

from bifrost_research.copilot.agents._context import utc_now_iso
from bifrost_research.copilot.harness.policy_schema import ResolutionPolicy, parse_policy
from bifrost_research.db.conn import rollback_quietly
from bifrost_research.repositories import ai_action_log as action_repo
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import hypothesis as hyp_repo
from bifrost_research.repositories import objective as obj_repo
from bifrost_research.schema.schemas import (
    TABLE_RESEARCH_CANDIDATE_OUTCOME,
    TABLE_RESEARCH_CANDIDATE_POOL,
)

logger = logging.getLogger(__name__)

ACTION_SOURCE = "eod_agent"
ACTION_KIND = "hypothesis_auto_resolve"
RULE_VERSION = 1

VALIDATED = "validated"
REJECTED = "rejected"
AMBIGUOUS = "ambiguous"
PENDING = "pending"
NO_LINEAGE = "no_lineage"


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def decide(excess: float | None, rule: ResolutionPolicy) -> tuple[str, str]:
    """The rule, as a pure function: ``(decision, reason)``."""
    if excess is None:
        return PENDING, f"horizon {rule.horizon_days} sessions not settled yet"
    if excess >= rule.validate_excess:
        return VALIDATED, (
            f"excess {excess:+.2%} vs {rule.benchmark} over {rule.horizon_days} sessions "
            f"≥ validate {rule.validate_excess:+.2%}"
        )
    if excess <= rule.reject_excess:
        return REJECTED, (
            f"excess {excess:+.2%} vs {rule.benchmark} over {rule.horizon_days} sessions "
            f"≤ reject {rule.reject_excess:+.2%}"
        )
    return AMBIGUOUS, (
        f"excess {excess:+.2%} vs {rule.benchmark} over {rule.horizon_days} sessions is inside "
        f"the dead band ({rule.reject_excess:+.2%} … {rule.validate_excess:+.2%}) — Owner decides"
    )


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def load_outcome(conn: _Connection, candidate_id: str, horizon_days: int) -> dict[str, Any] | None:
    """The settled forward window for one candidate at one horizon, or None."""
    sql = f"""
        SELECT o.candidate_id, c.symbol, c.trade_date, o.horizon_days, o.exit_date,
               o.entry_close, o.exit_close, o.forward_return, o.benchmark_symbol,
               o.benchmark_return, o.excess_return, o.hit, o.settled_at
        FROM {TABLE_RESEARCH_CANDIDATE_OUTCOME} o
        JOIN {TABLE_RESEARCH_CANDIDATE_POOL} c ON c.id = o.candidate_id
        WHERE o.candidate_id = %s AND o.horizon_days = %s
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (candidate_id, int(horizon_days)))
        row = cur.fetchone()
    if row is None:
        return None
    (
        cid, symbol, trade_date, horizon, exit_date, entry_close, exit_close,
        fwd, bench_sym, bench_ret, excess, hit, settled_at,
    ) = row
    return {
        "candidate_id": cid,
        "symbol": str(symbol or "").upper(),
        "trade_date": _iso(trade_date),
        "horizon_days": int(horizon),
        "exit_date": _iso(exit_date),
        "entry_close": _num(entry_close),
        "exit_close": _num(exit_close),
        "forward_return": _num(fwd),
        "benchmark_symbol": bench_sym,
        "benchmark_return": _num(bench_ret),
        "excess_return": _num(excess),
        "hit": hit,
        "settled_at": _iso(settled_at),
    }


def rule_for(
    conn: _Connection,
    hyp: dict[str, Any],
    cache: dict[str, ResolutionPolicy],
) -> ResolutionPolicy:
    """The objective's rule for this hypothesis, or the default rule.

    Lineage names the objective directly (new hypotheses) or through the run
    (older ones). A lookup that fails must not cost the hypothesis its review,
    so any miss falls back to the defaults, which is the rule the plan set.
    """
    ref = hyp.get("origin_ref") if isinstance(hyp.get("origin_ref"), dict) else {}
    objective_id = str(ref.get("objective_id") or "").strip()
    if not objective_id and ref.get("run_id"):
        try:
            run = obj_repo.get_run(conn, str(ref["run_id"]))
            objective_id = str((run or {}).get("objective_id") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.info("resolution: run lookup failed for %s: %s", hyp.get("id"), str(exc)[:120])
            rollback_quietly(conn)
    if not objective_id:
        return ResolutionPolicy()
    if objective_id in cache:
        return cache[objective_id]
    try:
        objective = obj_repo.get_objective(conn, objective_id) or {}
        rule = parse_policy(objective.get("policy_json") or {}).resolution
    except Exception as exc:  # noqa: BLE001
        logger.info("resolution: objective lookup failed for %s: %s", objective_id, str(exc)[:120])
        rollback_quietly(conn)
        rule = ResolutionPolicy()
    cache[objective_id] = rule
    return rule


def resolution_record(
    hyp: dict[str, Any],
    outcome: dict[str, Any],
    rule: ResolutionPolicy,
    decision: str,
    reason: str,
) -> dict[str, Any]:
    """What goes into ``hypothesis.resolution_json``: the receipt."""
    return {
        "rule_version": RULE_VERSION,
        "resolved_at": utc_now_iso(),
        "resolved_by": ACTION_SOURCE,
        "decision": decision,
        "reason": reason,
        "rule": rule.model_dump(),
        "outcome": outcome,
        "lineage": hyp.get("origin_ref") if isinstance(hyp.get("origin_ref"), dict) else None,
    }


def _conclusion(hyp: dict[str, Any], decision: str, reason: str, outcome: dict[str, Any]) -> str:
    title = str(hyp.get("title") or hyp.get("id"))
    verb = "held up" if decision == VALIDATED else "did not hold up"
    return (
        f"{title} {verb} by outcome rule: {reason}. "
        f"Entry {outcome.get('trade_date')} → exit {outcome.get('exit_date')}, "
        f"return {(_num(outcome.get('forward_return')) or 0.0):+.2%} vs "
        f"{outcome.get('benchmark_symbol') or 'benchmark'} "
        f"{(_num(outcome.get('benchmark_return')) or 0.0):+.2%}."
    )


def resolve_active(
    conn: _Connection,
    *,
    dry_run: bool = False,
    horizon_override: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Apply the outcome rule to every active hypothesis with candidate lineage.

    Returns a summary with one entry per hypothesis looked at. With
    ``dry_run`` nothing is written and the entries say what would happen —
    also the way to read a shorter horizon than the policy's for a preview.
    """
    active = hyp_repo.list_hypotheses(conn, status="active", limit=limit)
    cache: dict[str, ResolutionPolicy] = {}
    entries: list[dict[str, Any]] = []
    counts = {VALIDATED: 0, REJECTED: 0, AMBIGUOUS: 0, PENDING: 0, NO_LINEAGE: 0}
    applied: list[str] = []

    for hyp in active:
        hid = str(hyp.get("id"))
        ref = hyp.get("origin_ref") if isinstance(hyp.get("origin_ref"), dict) else {}
        candidate_id = str(ref.get("candidate_id") or "").strip()
        if not candidate_id:
            counts[NO_LINEAGE] += 1
            entries.append({"id": hid, "decision": NO_LINEAGE, "reason": "no candidate lineage"})
            continue

        rule = rule_for(conn, hyp, cache)
        if horizon_override is not None:
            rule = rule.model_copy(update={"horizon_days": int(horizon_override)})
        if not rule.enabled:
            counts[NO_LINEAGE] += 1
            entries.append({"id": hid, "decision": NO_LINEAGE, "reason": "resolution rule disabled"})
            continue

        try:
            outcome = load_outcome(conn, candidate_id, rule.horizon_days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolution: outcome read failed for %s: %s", hid, str(exc)[:160])
            rollback_quietly(conn)
            outcome = None
        excess = _num(outcome.get("excess_return")) if outcome else None
        decision, reason = decide(excess, rule)
        counts[decision] += 1
        entry: dict[str, Any] = {
            "id": hid,
            "symbol": (hyp.get("symbols") or [None])[0],
            "candidate_id": candidate_id,
            "horizon_days": rule.horizon_days,
            "excess_return": excess,
            "decision": decision,
            "reason": reason,
        }
        entries.append(entry)

        if decision not in (VALIDATED, REJECTED) or dry_run or outcome is None:
            continue

        record = resolution_record(hyp, outcome, rule, decision, reason)
        conclusion = _conclusion(hyp, decision, reason, outcome)
        try:
            updated = hyp_repo.resolve_hypothesis(
                conn, hid, status=decision, conclusion=conclusion, resolution=record
            )
            if updated is None:
                entry["applied"] = False
                entry["note"] = "hypothesis no longer active"
                continue
            action = action_repo.insert_action(
                conn,
                action_kind=ACTION_KIND,
                action_source=ACTION_SOURCE,
                model="outcome_rule",
                input_payload={"hypothesis_id": hid, "candidate_id": candidate_id, "rule": rule.model_dump()},
                output_payload={"status": decision, "reason": reason, "outcome": outcome},
                status="executed",
            )
            draft_repo.insert_draft(
                conn,
                kind="eod_verdict",
                payload={
                    "hypothesis_id": hid,
                    "hypothesis_title": hyp.get("title"),
                    "symbols": hyp.get("symbols") or [],
                    "proposed_status": decision,
                    "applied": True,
                    "auto_resolved": True,
                    "rationale": conclusion,
                    "markdown": f"- Resolved by outcome rule: **{decision}**\n- {reason}",
                    "bullets": [f"Resolved by outcome rule: {decision}", reason],
                    "resolution": record,
                    "model": "outcome_rule",
                    "generated_at": utc_now_iso(),
                },
                scope=hid,
                generated_by=ACTION_SOURCE,
                linked_action_id=action["id"],
            )
            entry["applied"] = True
            applied.append(hid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolution: applying %s to %s failed: %s", decision, hid, str(exc)[:160])
            rollback_quietly(conn)
            entry["applied"] = False
            entry["error"] = str(exc)[:200]

    return {
        "ok": True,
        "dry_run": dry_run,
        "active": len(active),
        "counts": counts,
        "applied": applied,
        "entries": entries,
        "advisory": "D10 BLOCKED — resolved hypotheses are findings, never orders.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve candidate-born hypotheses by outcome rule")
    parser.add_argument("--dry-run", action="store_true", help="Decide but write nothing")
    parser.add_argument("--horizon", type=int, default=None, help="Preview with this horizon instead of the policy's")
    args = parser.parse_args(argv)

    from bifrost_research.db.conn import connect

    conn = connect()
    try:
        result = resolve_active(conn, dry_run=args.dry_run, horizon_override=args.horizon)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
