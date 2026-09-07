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

#: Daily purses per provider, as the CronJob and the API pod set them. Shown
#: beside today's spend; a purse that is spent means fallback judges and held
#: candidates, which the Owner should see before the run says so.
DEFAULT_CAPS_USD = {"deepseek": 1.50, "openai": 2.00}

TRACK_RECORD_DAYS = 90


def _as_map(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


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

    for scope, kwargs in (("objective", {"objective_id": objective_id}), ("source", {"source": "harness"})):
        try:
            summary = build_summary(conn, days=TRACK_RECORD_DAYS, **kwargs)
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
            "days": TRACK_RECORD_DAYS,
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
    if not parts:
        # A policy with no legible filters — the description is the only thing
        # that says what this one is for.
        desc = str(obj.get("description") or "").strip()
        if desc:
            parts.append(desc[:120])
    n = policy.get("max_candidates")
    if n:
        parts.append(f"up to {n} a run")
    return " · ".join(parts)


def objective_standing(conn: Any, obj: dict[str, Any], runs: list[dict[str, Any]], pending_memos: int) -> dict[str, Any]:
    objective_id = str(obj.get("id") or "")
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
        "pending_memos": pending_memos,
        "runs": len(runs),
    }


def pending_memos_by_objective(conn: Any) -> dict[str, int]:
    from bifrost_research.repositories import ai_draft as draft_repo

    counts: dict[str, int] = {}
    try:
        rows = draft_repo.list_drafts(conn, status="pending", kind="candidate_batch", limit=200)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending memo count failed: %s", exc)
        return counts
    for r in rows:
        oid = str(_as_map(r.get("payload")).get("objective_id") or "")
        if oid:
            counts[oid] = counts.get(oid, 0) + 1
    return counts


def all_standings(conn: Any, *, status: str = "active", runs_per_objective: int = 30) -> list[dict[str, Any]]:
    from bifrost_research.repositories import objective as obj_repo

    objectives = obj_repo.list_objectives(conn, status=status, limit=50)
    pending = pending_memos_by_objective(conn)
    out: list[dict[str, Any]] = []
    for obj in objectives:
        oid = str(obj.get("id") or "")
        runs = obj_repo.list_runs(conn, objective_id=oid, limit=runs_per_objective)
        out.append(objective_standing(conn, obj, runs, pending.get(oid, 0)))
    return out


def purse_today(conn: Any) -> dict[str, Any]:
    from bifrost_research.repositories import ai_action_log as log_repo

    try:
        spent = log_repo.spend_today_by_provider(conn, action_kind="persona_eval_spend")
    except Exception as exc:  # noqa: BLE001
        logger.warning("purse read failed: %s", exc)
        spent = {}
    providers = []
    for name, cap in DEFAULT_CAPS_USD.items():
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
        "best_conviction": max((s.get("last_memo") or {}).get("best_conviction", 0) or 0 for s in standings) if standings else 0,
        "objectives": standings,
    }


__all__ = [
    "all_standings",
    "autopilot_standing",
    "hunts_line",
    "last_memo",
    "next_scheduled_run",
    "objective_standing",
    "purse_today",
    "spend_30d",
    "track_record",
]
