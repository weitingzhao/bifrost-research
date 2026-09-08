"""What the autopilot is, read as standing rather than as a table of runs.

The Harness Console listed objectives as rows — title, schedule, run count,
status — and a run table under each. That is how the loop works, not what it
is. What the Owner wants to know about an autopilot is what it hunts, what it
said last, whether its picks have been right, and what it costs; and about the
autopilot as a whole, whether it is trusted, when it runs next, what it has
spent today and how many memos are waiting. All of that already exists; it was
never assembled on one page.

Everything here is a read. The track record comes from ``candidate_outcome``,
the memo from the last run's ratings, the purse from the spend ledger, trust
from the cluster matrix. No model is called.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from bifrost_research.copilot.harness.rating import rating_summary
from bifrost_research.copilot.harness.trust_gate import matrix_level

logger = logging.getLogger(__name__)

#: The harness CronJob's schedule (k8s/engines/cronjob-harness.yaml): weekdays
#: 13:30 UTC, which is 09:30 New York in summer. Read once here so the page
#: can say when the next unattended run is without asking the cluster.
CRON_HOUR_UTC = 13
CRON_MINUTE_UTC = 30

#: The providers whose purses the page shows. The amounts are read from the
#: same environment the judge reads, never copied: a hardcoded pair here said
#: $1.50/$2.00 while the manifests were rebalanced to $2.25/$1.25, and the
#: page would have reported a ceiling the run does not obey.
PURSE_PROVIDERS = ("deepseek", "openai")

TRACK_RECORD_DAYS = 90
#: The harness-wide fallback reads two years — effectively the whole ledger —
#: to match the memo's own source-record line, which has no window at all.
SOURCE_RECORD_DAYS = 730


def _as_map(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


DEFAULT_SCAN_MODE = "scan_legacy"

#: What each universe mode reads, in the Owner's terms rather than the resolver's.
_UNIVERSE_LABEL = {
    "scan_legacy": "option scan snapshot",
    "sepa": "SEPA universe",
    "momentum": "momentum universe",
    "events": "events universe",
    "stock_composite": "stock composite funnel",
}


def _clip(text: str, limit: int) -> str:
    """Trim to ``limit`` on a word boundary, with an ellipsis when cut."""
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return f"{head or text[:limit]}…"


def next_scheduled_run(now: datetime | None = None) -> str:
    """ISO timestamp of the next weekday 13:30 UTC strictly after ``now``."""
    now = now or datetime.now(UTC)
    candidate = now.replace(hour=CRON_HOUR_UTC, minute=CRON_MINUTE_UTC, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.isoformat()


def last_memo(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest run that carries ratings, summarised for the row."""
    for run in runs:
        out = _as_map(run.get("outputs"))
        ratings = out.get("ratings") if isinstance(out.get("ratings"), list) else []
        if not ratings:
            continue
        considered = None
        trace = _as_map(run.get("trace_json"))
        for ev in trace.get("events") or []:
            if isinstance(ev, dict) and ev.get("step") == "scan_universe":
                funnel = ev.get("funnel") if isinstance(ev.get("funnel"), list) else []
                first = funnel[0] if funnel and isinstance(funnel[0], dict) else {}
                try:
                    considered = int(first.get("in_count"))
                except (TypeError, ValueError):
                    considered = None
                break
        s = rating_summary(ratings, considered=considered)
        return {
            "run_id": run.get("id"),
            "started_at": run.get("started_at"),
            "status": run.get("status"),
            "headline": s["headline"],
            "best_conviction": s["best"],
            "actionable": s["actionable"],
            "split": s["split"],
            "blocked": s["blocked"],
            "picks": s["picks"],
            "considered": s["considered"],
        }
    return None


def _best_horizon(summary: dict[str, Any]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for h in summary.get("horizons") or []:
        if not isinstance(h, dict) or int(h.get("judged") or 0) <= 0:
            continue
        if best is None or int(h.get("horizon_days") or 0) > int(best.get("horizon_days") or 0):
            best = h
    return best


def track_record(conn: Any, objective_id: str) -> dict[str, Any]:
    """Hit rate vs SPY at the longest judged horizon, for this objective's picks.

    Scoped to the objective when its own picks have settled. Candidates from
    before the objective id was written into the pool cannot be attributed, so
    when nothing objective-scoped has settled the harness-wide record is shown
    instead and labelled as such — a number with the wrong scope is worse than
    none, but none at all hides a record the Owner has paid to settle.
    """
    from bifrost_research.api.candidate_outcome import build_summary

    # The memo's own "source record" line reads the harness-wide ledger with no
    # window; if this row used a 90-day window for the fallback the two would
    # disagree about the same eight outcomes. Same scope, same window.
    for scope, days, kwargs in (
        ("objective", TRACK_RECORD_DAYS, {"objective_id": objective_id}),
        ("source", SOURCE_RECORD_DAYS, {"source": "harness"}),
    ):
        try:
            summary = build_summary(conn, days=days, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("track record (%s) for %s failed: %s", scope, objective_id, exc)
            continue
        best = _best_horizon(summary)
        if best is None:
            continue
        return {
            "status": "ok",
            "scope": scope,
            "horizon_days": int(best.get("horizon_days") or 0),
            "hit_rate": best.get("hit_rate"),
            "judged": int(best.get("judged") or 0),
            "avg_excess": best.get("avg_excess"),
            "pending": int(summary.get("pending") or 0),
            "days": days,
        }
    return {"status": "none_settled", "scope": None, "horizon_days": None, "hit_rate": None, "judged": 0, "avg_excess": None}


def spend_30d(runs: list[dict[str, Any]]) -> float:
    """Judge and planner spend across the runs given, from each run's own record."""
    total = 0.0
    for run in runs:
        out = _as_map(run.get("outputs"))
        for m in (_as_map(out.get("persona_eval")).get("models") or []):
            if isinstance(m, dict) and isinstance(m.get("cost_usd"), (int, float)):
                total += float(m["cost_usd"])
        t = _as_map(out.get("triage"))
        if isinstance(t.get("cost_usd"), (int, float)):
            total += float(t["cost_usd"])
        for a in (_as_map(run.get("plan_json")).get("llm_attempts") or []):
            if isinstance(a, dict) and isinstance(a.get("cost_usd"), (int, float)):
                total += float(a["cost_usd"])
    return round(total, 4)


def hunts_line(obj: dict[str, Any]) -> str:
    """What this objective looks for, in one line, from its policy."""
    policy = _as_map(obj.get("policy_json"))
    mode = str(policy.get("universe_mode") or "")
    parts: list[str] = []
    if mode == "stock_composite":
        layers = _as_map(policy.get("layers"))
        sepa = _as_map(layers.get("sepa"))
        stages = sepa.get("stage")
        if isinstance(stages, list) and stages:
            parts.append("/".join(str(s) for s in stages) + " names")
        if sepa.get("min_score") is not None:
            parts.append(f"SEPA ≥ {sepa.get('min_score'):g}")
        overlay = _as_map(policy.get("option_overlay"))
        if overlay.get("enabled"):
            flag = overlay.get("flag_filter")
            parts.append(f"option overlay {flag}" if flag else "option overlay")
    else:
        flag = policy.get("flag_filter")
        if isinstance(flag, list):
            flag = ", ".join(str(f) for f in flag)
        if flag:
            parts.append(str(flag))
        preset = policy.get("preset")
        if preset and preset != "neutral":
            parts.append(f"{preset} preset")
        floor = policy.get("min_composite_score")
        if floor is not None:
            parts.append(f"composite ≥ {float(floor):g}")
        if not parts:
            # Nothing filters: the run ranks its universe and takes the top N.
            # Saying that is more honest than falling back to the description,
            # which is written by hand and can promise a screen the policy does
            # not run — one objective claimed to hunt IV rank ≥ 90 while its
            # policy held no filter at all.
            parts.append(f"{_UNIVERSE_LABEL.get(mode or DEFAULT_SCAN_MODE, mode)} · ranked, not screened")
    if not parts:
        # Still nothing legible — the description is all there is. Cut on a word
        # so the line does not end mid-syllable.
        parts.append(_clip(str(obj.get("description") or "").strip(), 110))
    n = policy.get("max_candidates")
    if n:
        parts.append(f"up to {n} a run")
    return " · ".join(parts)


def objective_standing(
    conn: Any,
    obj: dict[str, Any],
    runs: list[dict[str, Any]],
    pending: dict[str, int] | int,
) -> dict[str, Any]:
    objective_id = str(obj.get("id") or "")
    # An int still works: the count is the call count, with no folded repeats.
    slot = pending if isinstance(pending, dict) else {"calls": int(pending or 0), "drafts": int(pending or 0)}
    calls = int(slot.get("calls", 0))
    drafts = int(slot.get("drafts", calls))
    latest = runs[0] if runs else None
    return {
        "id": objective_id,
        "title": obj.get("title"),
        "status": obj.get("status"),
        "schedule": obj.get("schedule"),
        "hunts": hunts_line(obj),
        "last_run": {
            "id": latest.get("id"),
            "started_at": latest.get("started_at"),
            "finished_at": latest.get("finished_at"),
            "status": latest.get("status"),
        }
        if latest
        else None,
        "last_memo": last_memo(runs),
        "track_record": track_record(conn, objective_id),
        "spend_30d_usd": spend_30d(runs),
        "pending_memos": calls,
        # The rows behind those calls — `drafts - pending_memos` were folded as
        # repeats of the same names.
        "pending_drafts": drafts,
        "runs": len(runs),
    }


def batch_call_key(payload: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    """What makes two candidate batches the same call: objective and names.

    Mirrors the Decision Inbox's own grouping key
    (``harnessDraftHelpers._batchKey``). Re-running an objective through the
    day proposes the same eight symbols again; those are one decision, and the
    Inbox has folded them since the "25 to decide" fix. This side had not, so
    the Autopilot page said twenty-one memos were waiting while the Inbox
    offered three calls — the same queue, counted two ways.
    """
    oid = str(payload.get("objective_id") or "")
    items = payload.get("items")
    symbols = sorted(
        str(i.get("symbol") or "").upper()
        for i in (items if isinstance(items, list) else [])
        if isinstance(i, dict) and i.get("symbol")
    )
    return oid, tuple(symbols)


def pending_memos_by_objective(conn: Any) -> dict[str, dict[str, int]]:
    """Per objective: ``calls`` (distinct decisions) and ``drafts`` (rows).

    ``calls`` is the number the Owner is asked to act on; ``drafts`` is kept
    beside it so a folded repeat is visible rather than silently missing.
    """
    from bifrost_research.repositories import ai_draft as draft_repo

    counts: dict[str, dict[str, int]] = {}
    try:
        rows = draft_repo.list_drafts(conn, status="pending", kind="candidate_batch", limit=200)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending memo count failed: %s", exc)
        return counts
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for r in rows:
        payload = _as_map(r.get("payload"))
        oid = str(payload.get("objective_id") or "")
        if not oid:
            continue
        slot = counts.setdefault(oid, {"calls": 0, "drafts": 0})
        slot["drafts"] += 1
        key = batch_call_key(payload)
        # A batch with no symbols cannot be matched to another; it stands alone.
        if not key[1]:
            slot["calls"] += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        slot["calls"] += 1
    return counts


def all_standings(conn: Any, *, status: str = "active", runs_per_objective: int = 30) -> list[dict[str, Any]]:
    from bifrost_research.repositories import objective as obj_repo

    objectives = obj_repo.list_objectives(conn, status=status, limit=50)
    pending = pending_memos_by_objective(conn)
    out: list[dict[str, Any]] = []
    for obj in objectives:
        oid = str(obj.get("id") or "")
        runs = obj_repo.list_runs(conn, objective_id=oid, limit=runs_per_objective)
        out.append(objective_standing(conn, obj, runs, pending.get(oid, {"calls": 0, "drafts": 0})))
    return out


def purse_today(conn: Any) -> dict[str, Any]:
    from bifrost_research.repositories import ai_action_log as log_repo

    try:
        spent = log_repo.spend_today_by_provider(conn, action_kind="persona_eval_spend")
    except Exception as exc:  # noqa: BLE001
        logger.warning("purse read failed: %s", exc)
        spent = {}
    providers = []
    from bifrost_research.copilot import rate_limit

    for name in PURSE_PROVIDERS:
        cap = rate_limit.provider_cap_usd(name)
        used = round(float(spent.get(name, 0.0)), 4)
        providers.append({"provider": name, "spent_usd": used, "cap_usd": cap, "exhausted": used >= cap})
    return {
        "spent_usd": round(sum(p["spent_usd"] for p in providers), 4),
        "cap_usd": round(sum(p["cap_usd"] for p in providers), 2),
        "providers": providers,
    }


def autopilot_standing(conn: Any) -> dict[str, Any]:
    """The page-level facts: trust, next run, purse, memos waiting."""
    standings = all_standings(conn)
    level = matrix_level()
    return {
        "trust": {
            "matrix_level": level,
            "matrix_l0": level == "L0",
            "source": "cluster platform-api",
            "note": (
                "Auto-accept is armed for the next unattended run."
                if level == "L0"
                else "Auto-accept stays off until Research · Loop Batch is L0 on the cluster matrix."
                if level
                else "Trust matrix unreachable; the loop will hold everything."
            ),
        },
        "next_run_at": next_scheduled_run(),
        "purse": purse_today(conn),
        "pending_memos": sum(s.get("pending_memos", 0) for s in standings),
        "pending_drafts": sum(s.get("pending_drafts", 0) for s in standings),
        "best_conviction": max((s.get("last_memo") or {}).get("best_conviction", 0) or 0 for s in standings) if standings else 0,
        "objectives": standings,
    }


__all__ = [
    "all_standings",
    "autopilot_standing",
    "batch_call_key",
    "hunts_line",
    "last_memo",
    "next_scheduled_run",
    "objective_standing",
    "purse_today",
    "spend_30d",
    "track_record",
]
