"""A name the Owner declined comes back only when something got better.

The loop had no record of a refusal. Dismissing a Decision Inbox draft wrote
`research.ai_draft` and nothing else, so `research.candidate_pool` — the table
the loop actually reads — never learned. Three days of running proposed twelve
distinct symbols; 2026-09-06 and 2026-09-07 were the same eleven; seven names
came back every single day after being declined every single day.

This module answers one question, with no I/O: given what a symbol looked like
on the day it was declined and what it looks like now, has anything material
improved? It is a pure stage in the shape of ``leash.py`` and ``triage.py`` —
the run reads the rows, this decides, the funnel records what it decided.

Two rules that look like details and are not:

* **Only improvement counts.** A score that fell from 78 to 70 is not news
  that overturns a refusal; it is a worse version of the name that was
  refused. Symmetric thresholds would hand the Owner back everything that
  merely moved.
* **Nothing to compare means stay silent.** When either side has no snapshot,
  the run cannot show that anything changed, and re-proposal needs positive
  evidence. The absence is on the permission side, not the measurement side:
  "we lost the evidence" is not grounds to overrule a refusal.

D10 BLOCKED — advisory research only.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Where a name sits on the SEPA path, worst to best. From
#: ``sepa_fusion._classify_path``: STAGE_4 → AVOID, STAGE_3 → WATCH,
#: STAGE_2C → EXTENDED, STAGE_2A/2B → WATCH / SETUP / PIVOT by score.
#: EXTENDED ranks below SETUP on purpose — the buy point is behind it, so
#: PIVOT → EXTENDED is the setup running away, not improving.
PATH_RANK: dict[str, int] = {"AVOID": 0, "WATCH": 1, "EXTENDED": 2, "SETUP": 3, "PIVOT": 4}

#: ``sepa_fusion._grade_from_score`` bands.
GRADE_RANK: dict[str, int] = {"D": 0, "C": 1, "B": 2, "A": 3, "A+": 4}

#: Fields read off a `lens_snapshot`, in the order `_primary_score` prefers.
_SCORE_KEYS = ("sepa_score", "option_composite", "composite_score", "momentum_score", "score")

DEFAULT_MIN_SCORE_DELTA = 5.0
DEFAULT_LOOKBACK_DAYS = 90


def primary_score(snapshot: dict[str, Any] | None) -> float | None:
    """The score this snapshot is judged on.

    Mirrors ``runtime._primary_score`` deliberately: both sides of every
    comparison must be read off the same ruler, and a snapshot carries the
    same keys the runtime picks from.
    """
    if not snapshot:
        return None
    for key in _SCORE_KEYS:
        val = snapshot.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def _text(snapshot: dict[str, Any] | None, key: str) -> str | None:
    if not snapshot:
        return None
    raw = snapshot.get(key)
    if raw is None:
        return None
    val = str(raw).strip().upper()
    return val or None


def _int(snapshot: dict[str, Any] | None, key: str) -> int | None:
    if not snapshot:
        return None
    try:
        return int(snapshot.get(key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def changes_since(
    then: dict[str, Any] | None,
    now: dict[str, Any] | None,
    *,
    min_score_delta: float = DEFAULT_MIN_SCORE_DELTA,
    min_event_importance: int = 2,
) -> list[dict[str, Any]]:
    """What improved between the declined snapshot and today's, if anything.

    Returns one entry per rule that fired. An empty list means the name reads
    the same as the day it was refused — or that there was nothing to compare,
    which callers must treat the same way.
    """
    if not then or not now:
        return []

    out: list[dict[str, Any]] = []

    before, after = primary_score(then), primary_score(now)
    if before is not None and after is not None and (after - before) >= min_score_delta:
        out.append(
            {
                "rule": "score_improved",
                "field": "score",
                "from": round(before, 1),
                "to": round(after, 1),
            }
        )

    p_then, p_now = _text(then, "path"), _text(now, "path")
    if p_then in PATH_RANK and p_now in PATH_RANK and PATH_RANK[p_now] > PATH_RANK[p_then]:
        out.append({"rule": "path_advanced", "field": "path", "from": p_then, "to": p_now})

    g_then, g_now = _text(then, "grade"), _text(now, "grade")
    if g_then in GRADE_RANK and g_now in GRADE_RANK and GRADE_RANK[g_now] > GRADE_RANK[g_then]:
        out.append({"rule": "grade_improved", "field": "grade", "from": g_then, "to": g_now})

    # A new event only counts if it clears the same bar the events layer uses;
    # a second threshold here would drift from the one the funnel applies.
    e_then, e_now = _text(then, "event_date"), _text(now, "event_date")
    importance = _int(now, "event_importance")
    if e_now and e_now != e_then and (importance is None or importance >= min_event_importance):
        out.append({"rule": "new_event", "field": "event_date", "from": e_then, "to": e_now})

    r_then, r_now = _text(then, "terrain_regime"), _text(now, "terrain_regime")
    if r_then and r_now and r_then != r_now:
        out.append(
            {"rule": "regime_flip", "field": "terrain_regime", "from": r_then, "to": r_now}
        )

    return out


def change_phrase(change: dict[str, Any]) -> str:
    """One change in the Owner's words: ``SEPA 78 → 84``, ``now PIVOT``."""
    rule = change.get("rule")
    if rule == "score_improved":
        return f"score {change.get('from')} → {change.get('to')}"
    if rule == "path_advanced":
        return f"now {change.get('to')}"
    if rule == "grade_improved":
        return f"grade {change.get('from')} → {change.get('to')}"
    if rule == "new_event":
        return f"event {change.get('to')}"
    if rule == "regime_flip":
        return f"regime {change.get('from')} → {change.get('to')}"
    return str(change.get("field") or rule or "changed")


def summarise(declined_on: str | None, changes: list[dict[str, Any]]) -> str:
    """``declined 09-04; score 78 → 84, now PIVOT``."""
    when = f"declined {declined_on[5:]}" if declined_on and len(declined_on) >= 10 else "declined"
    if not changes:
        return f"{when}; nothing to compare"
    return f"{when}; " + ", ".join(change_phrase(c) for c in changes)


def suppress_declined(
    symbols: list[str],
    *,
    snapshots_now: dict[str, dict[str, Any]],
    declines: dict[str, dict[str, Any]],
    decided_today: dict[str, dict[str, Any]] | None = None,
    min_score_delta: float = DEFAULT_MIN_SCORE_DELTA,
    min_event_importance: int = 2,
) -> dict[str, Any]:
    """Split today's symbols into the ones worth proposing and the ones not.

    ``declines`` is the newest dismissed pool row per symbol; ``decided_today``
    is any row already decided in this session, dismissed or promoted — a name
    settled hours ago must not come back on the afternoon run whatever moved.

    Returns ``symbols`` (keep, original order), ``returning`` (kept names that
    had been declined, with what changed), and ``suppressed`` (dropped names
    with the reason each was dropped).
    """
    decided_today = decided_today or {}
    keep: list[str] = []
    returning: dict[str, dict[str, Any]] = {}
    suppressed: list[dict[str, Any]] = []

    for sym in symbols:
        settled = decided_today.get(sym)
        if settled is not None:
            suppressed.append(
                {
                    "symbol": sym,
                    "reason": f"already decided today ({settled.get('status')})",
                    "declined_on": str(settled.get("trade_date") or "")[:10] or None,
                }
            )
            continue

        prior = declines.get(sym)
        if prior is None:
            keep.append(sym)
            continue

        declined_on = str(prior.get("trade_date") or "")[:10] or None
        then = prior.get("lens_snapshot") if isinstance(prior.get("lens_snapshot"), dict) else None
        now = snapshots_now.get(sym)
        changes = changes_since(
            then,
            now,
            min_score_delta=min_score_delta,
            min_event_importance=min_event_importance,
        )
        if not changes:
            reason = (
                "declined and nothing material changed"
                if then and now
                else "declined; no snapshot to compare"
            )
            suppressed.append({"symbol": sym, "reason": reason, "declined_on": declined_on})
            continue

        keep.append(sym)
        returning[sym] = {
            "declined_on": declined_on,
            "declined_candidate_id": prior.get("id"),
            "changes": changes,
            "summary": summarise(declined_on, changes),
        }

    return {"symbols": keep, "returning": returning, "suppressed": suppressed}


def decline_stage(
    conn: Any,
    symbols: list[str],
    *,
    objective: dict[str, Any],
    loop_policy: Any,
    run_id: str,
    data_source: str,
    row_meta_by_symbol: dict[str, Any],
    snapshot_for: Any,
) -> tuple[list[str], dict[str, Any]]:
    """The gate as the run uses it: read the refusals, decide, record.

    Extracted from ``runtime.run_objective`` for the same reason
    ``triage_stage`` and ``persona_stage`` were — the runtime is at its length
    limit and a stage that owns its own funnel step and trace event is easier
    to test than one spliced into a 700-line function.

    The caller applies this *before* truncating to ``max_candidates``. Suppress
    after the cut and a run capped at 8 that refused 7 proposes 1; suppress
    before it and the rest of the funnel takes the freed places, which is the
    point of remembering at all.

    Fails open, never closed. A lookup that raises suppresses nobody and says
    so in the funnel: a run that narrows silently on a database error is the
    failure this gate exists to prevent, pointed the other way.
    """
    from datetime import date

    from bifrost_research.copilot.harness.universe.types import FunnelStep
    from bifrost_research.db.conn import rollback_quietly
    from bifrost_research.repositories import candidate_pool as cand_repo

    policy = getattr(loop_policy, "decline_memory", None)
    enabled = bool(getattr(policy, "enabled", True))
    min_delta = float(getattr(policy, "min_score_delta", DEFAULT_MIN_SCORE_DELTA))
    lookback = int(getattr(policy, "lookback_days", DEFAULT_LOOKBACK_DAYS))

    def view(kept: list[str], returning: dict, suppressed: list, skip: str | None) -> dict[str, Any]:
        step = FunnelStep(
            name="decline_memory",
            in_count=len(symbols),
            out_count=len(kept),
            filter_summary=(
                "declined and nothing material changed since — needs score "
                f"+{min_delta:g}, a path advance, a grade notch, a new qualifying "
                "event, or a regime flip"
            ),
            dropped_sample=[s["symbol"] for s in suppressed],
            optional=True,
            skipped=skip is not None,
            skip_reason=skip,
        ).to_dict()
        out: dict[str, Any] = {"funnel_step": step, "returning": returning, "suppressed": suppressed}
        if suppressed or returning:
            out["trace_event"] = {
                "step": "decline_memory",
                "label": "Decline memory",
                "suppressed": suppressed,
                "returning": sorted(returning),
                "decision": f"suppressed={len(suppressed)} returning={len(returning)}",
            }
        return out

    if not enabled:
        return symbols, view(symbols, {}, [], "policy.decline_memory.enabled = false")
    if not symbols:
        return symbols, view(symbols, {}, [], None)

    objective_id = str(objective.get("id") or "")
    try:
        declines = cand_repo.latest_by_symbol(
            conn, symbols, statuses=("dismissed",), objective_id=objective_id, days=lookback
        )
        decided_today = cand_repo.latest_by_symbol(
            conn,
            symbols,
            statuses=("dismissed", "promoted"),
            objective_id=objective_id,
            # Must match how `create_candidate` stamps `trade_date`, which is
            # `date.today()` on the same host — a UTC date would disagree with
            # the stored value for part of every day.
            trade_date=date.today(),  # noqa: DTZ011
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("decline lookup failed for run %s: %s", run_id, exc)
        rollback_quietly(conn)
        return symbols, view(symbols, {}, [], f"decline lookup failed: {str(exc)[:120]}")

    snapshots_now = {
        sym: snapshot_for(
            objective_id=objective_id,
            run_id=run_id,
            data_source=data_source,
            meta=row_meta_by_symbol.get(sym),
        )
        for sym in symbols
    }
    split = suppress_declined(
        symbols,
        snapshots_now=snapshots_now,
        declines=declines,
        decided_today=decided_today,
        min_score_delta=min_delta,
        min_event_importance=loop_policy.layers.events.min_importance,
    )
    return split["symbols"], view(split["symbols"], split["returning"], split["suppressed"], None)


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_MIN_SCORE_DELTA",
    "GRADE_RANK",
    "PATH_RANK",
    "change_phrase",
    "changes_since",
    "decline_stage",
    "primary_score",
    "summarise",
    "suppress_declined",
]
