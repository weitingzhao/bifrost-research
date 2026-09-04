"""LLM / outcome policy_suggestion diffs — Wave Y.3 A1 + Wave 5 flywheel.

Given an LLM plan (with an optional ``policy_suggestion``) and the current
objective ``policy_json``, produce the actionable diff that becomes the
payload of a ``policy_suggestion`` Decision Inbox draft.

Wave 5 adds ``policy_suggestion_from_outcomes`` when Persona eval shows
repeated validate oppose / weak net_stance — still Inbox-only (never
auto-approved). Wave 5.1 also folds in ``research.candidate_outcome``
summary hit-rates when available.

Only keys in ``plan_llm.POLICY_SUGGESTION_KEYS`` may cross into a draft
(defense in depth — Pydantic already filtered at plan-generation time).
Only keys whose *value* differs from the current policy are surfaced.

D10 BLOCKED — this module builds a proposal dict; the actual write onto
``objective.policy_json`` happens in ``api.agents.approve_draft`` after
Owner approval, via ``repositories.objective.patch_policy_json``.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.copilot.harness import plan_llm


def _diff_against_current(
    proposed: dict[str, Any],
    current_policy: dict[str, Any] | None,
) -> dict[str, Any] | None:
    current = current_policy or {}
    diff: dict[str, Any] = {}
    for key in plan_llm.POLICY_SUGGESTION_KEYS:
        if key not in proposed:
            continue
        value = proposed[key]
        if current.get(key) != value:
            diff[key] = value
    return diff or None


def policy_suggestion_from_plan(
    plan: dict[str, Any],
    current_policy: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return an actionable diff, or ``None`` when nothing changes.

    - ``plan.policy_suggestion`` must be a non-empty dict.
    - Keys outside ``POLICY_SUGGESTION_KEYS`` are ignored (should already
      be filtered by ``LLMPlanResponse``).
    - Keys whose value equals ``current_policy.get(key)`` are dropped.
    - Empty diff → ``None`` (runtime skips draft creation).
    """
    raw = plan.get("policy_suggestion")
    if not isinstance(raw, dict) or not raw:
        return None
    return _diff_against_current(raw, current_policy)


def _weak_outcome_horizon(outcome_summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the weakest judged horizon when hit_rate is clearly soft."""
    if not isinstance(outcome_summary, dict):
        return None
    horizons = outcome_summary.get("horizons")
    if not isinstance(horizons, list):
        return None
    weak: dict[str, Any] | None = None
    for h in horizons:
        if not isinstance(h, dict):
            continue
        judged = int(h.get("judged") or 0)
        hr = h.get("hit_rate")
        if judged < 5 or hr is None:
            continue
        try:
            rate = float(hr)
        except (TypeError, ValueError):
            continue
        if rate >= 0.40:
            continue
        if weak is None or rate < float(weak.get("hit_rate") or 1.0):
            weak = {
                "horizon_days": h.get("horizon_days"),
                "hit_rate": rate,
                "judged": judged,
            }
    return weak


def policy_suggestion_from_outcomes(
    persona_eval: dict[str, Any] | None,
    *,
    current_policy: dict[str, Any] | None,
    outcome_summary: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Wave 5 — suggest tightening Policy when validate dissent or settled outcomes weaken.

    Returns ``{"suggestion": {...}, "reasoning": "..."}`` or None.
    Never auto-applied; Owner must approve in Inbox.
    """
    current = dict(current_policy or {})
    proposed: dict[str, Any] = {}
    reasoning_parts: list[str] = []

    evaluated = 0
    blocked = 0
    if isinstance(persona_eval, dict):
        evaluated = int(persona_eval.get("symbols_evaluated") or 0)
        blocked = int(persona_eval.get("blocked_by_validate") or 0)
        if evaluated >= 2 and blocked >= max(1, evaluated // 2):
            reasoning_parts.append(
                f"Persona eval blocked {blocked}/{evaluated} candidates via validate oppose."
            )

    weak = _weak_outcome_horizon(outcome_summary)
    if weak is not None:
        reasoning_parts.append(
            f"Settled candidate_outcome weak at T+{weak.get('horizon_days')} "
            f"(hit_rate={float(weak['hit_rate']):.0%} on {weak['judged']} judged)."
        )

    if not reasoning_parts:
        return None

    # Raise SEPA floor when stock composite / layers present
    layers = current.get("layers") if isinstance(current.get("layers"), dict) else {}
    sepa = layers.get("sepa") if isinstance(layers.get("sepa"), dict) else {}
    try:
        min_score = float(sepa.get("min_score", 70))
    except (TypeError, ValueError):
        min_score = 70.0
    bump = 5.0 if weak is None else 8.0
    new_min = min(95.0, min_score + bump)
    if new_min > min_score:
        new_layers = {**layers, "sepa": {**sepa, "min_score": new_min}}
        proposed["layers"] = new_layers
        reasoning_parts.append(f"Raise layers.sepa.min_score {min_score} → {new_min}.")

    # Or raise composite floor for scan_legacy
    if current.get("universe_mode") == "scan_legacy" or not proposed:
        try:
            mcs = (
                float(current["min_composite_score"])
                if current.get("min_composite_score") is not None
                else None
            )
        except (TypeError, ValueError):
            mcs = None
        if mcs is not None and mcs <= 100:
            step = 5.0 if mcs > 1 else 0.05
            if weak is not None:
                step = 8.0 if mcs > 1 else 0.08
            bump_mcs = min(100.0, mcs + step)
            if bump_mcs != mcs:
                proposed["min_composite_score"] = bump_mcs
                reasoning_parts.append(f"Raise min_composite_score {mcs} → {bump_mcs}.")

    # Soften batch size when outcomes are weak
    if weak is not None:
        try:
            mc = int(current.get("max_candidates") or 0)
        except (TypeError, ValueError):
            mc = 0
        if mc > 3:
            new_mc = max(3, mc - 1)
            if new_mc != mc:
                proposed["max_candidates"] = new_mc
                reasoning_parts.append(f"Lower max_candidates {mc} → {new_mc}.")

    proposed["require_validate_pass"] = True
    reasoning_parts.append("Keep require_validate_pass=true (dissent must stay visible).")

    diff = _diff_against_current(proposed, current)
    if not diff:
        return None
    return {
        "suggestion": diff,
        "reasoning": " ".join(reasoning_parts),
    }


__all__ = [
    "policy_suggestion_from_outcomes",
    "policy_suggestion_from_plan",
]
