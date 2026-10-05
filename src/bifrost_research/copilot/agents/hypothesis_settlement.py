"""When a candidate-born hypothesis settles — ``settles_on`` on the hypothesis reads (0.168.0).

Owner plan decision #16 (REQUEST-design-rev154-alignment-plan-2026-10-04): the
Trade Calendar's "Hypothesis horizons" layer quotes a date Research derives,
rather than the page recomputing one the Hypotheses page does not show.

The date is the one ``hypothesis_resolution.resolve_active`` will act on, from
the same pieces:

- the rule is the objective's ``policy_json.resolution`` (``rule_from_objective``),
  found through the same lineage (``lineage_refs``: ``objective_id``, else the
  run's objective, else the default 20-session rule);
- the window is the one ``engines/candidate_outcome`` settles: entry on the
  first session on or after the candidate's ``trade_date``, exit
  ``horizon_days`` sessions later (``db/calendar.settlement_session``). Once
  the engine has written ``candidate_outcome.exit_date`` for that horizon, that
  date is quoted instead of the projection.

The EOD review that resolves runs the evening of the exit session (21:30 UTC,
after ``engines.candidate_outcome``). A hypothesis that cannot settle by rule
gets ``settles_on = null`` and a ``settles_basis.reason`` saying why.

Reads only, in bulk: at most one query each for candidates, runs, objectives
and outcomes, plus the trading calendar — never one per hypothesis.
D10 BLOCKED — a date on a finding, never an order.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from bifrost_research.copilot.agents.hypothesis_resolution import (
    lineage_refs,
    rule_from_objective,
)
from bifrost_research.copilot.harness.policy_schema import ResolutionPolicy
from bifrost_research.db.calendar import cached_closed_days, settlement_session
from bifrost_research.db.conn import rollback_quietly
from bifrost_research.engines.candidate_outcome.build import (
    DEFAULT_HORIZONS,
    DEFAULT_LOOKBACK_DAYS,
)
from bifrost_research.schema.schemas import (
    TABLE_RESEARCH_CANDIDATE_OUTCOME,
    TABLE_RESEARCH_CANDIDATE_POOL,
    TABLE_RESEARCH_OBJECTIVE,
    TABLE_RESEARCH_OBJECTIVE_RUN,
)

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")

FROM_OUTCOME = "candidate_outcome"
FROM_TRADE_DATE = "candidate_trade_date"
SOURCE_POLICY = "policy"
SOURCE_DEFAULT = "default"

REASON_RESOLVED = "resolved"
REASON_RETIRED = "retired"
REASON_NO_LINEAGE = "no_candidate_lineage"
REASON_RULE_DISABLED = "resolution_disabled"
REASON_HORIZON_NOT_SETTLED = "horizon_not_settled_by_engine"
REASON_CANDIDATE_MISSING = "candidate_not_found"
REASON_NO_TRADE_DATE = "candidate_trade_date_missing"
REASON_OUTSIDE_WINDOW = "outside_settlement_window"
REASON_LOOKUP_FAILED = "lookup_failed"


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _cell(row: Any, i: int, key: str) -> Any:
    return row.get(key) if isinstance(row, Mapping) else row[i]


def _select(conn: Any, sql: str, params: tuple[Any, ...]) -> list[Any]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall() or [])


def _read(conn: Any, what: str, sql: str, ids: Iterable[str]) -> list[Any] | None:
    """One ``= ANY(%s)`` read; None when it failed (the transaction is reset)."""
    keys = sorted({i for i in ids if i})
    if not keys:
        return []
    try:
        return _select(conn, sql, (keys,))
    except Exception as exc:  # noqa: BLE001
        logger.info("settlement: %s read failed: %s", what, str(exc)[:160])
        rollback_quietly(conn)
        return None


def _new_york_today() -> date:
    return datetime.now(_NY).date()


def _closed_reason(hyp: Mapping[str, Any]) -> dict[str, Any] | None:
    """A hypothesis that is no longer waiting on a window."""
    status = str(hyp.get("status") or "")
    if hyp.get("retired_at") or status == "archived":
        return {"reason": REASON_RETIRED, "status": status or None}
    if status != "active":
        basis: dict[str, Any] = {"reason": REASON_RESOLVED, "status": status or None}
        resolution = hyp.get("resolution_json")
        if isinstance(resolution, Mapping):
            outcome = resolution.get("outcome") if isinstance(resolution.get("outcome"), Mapping) else {}
            if outcome.get("exit_date"):
                basis["exit_date"] = str(outcome["exit_date"])[:10]
            if resolution.get("resolved_by"):
                basis["resolved_by"] = resolution["resolved_by"]
        return basis
    return None


def settlements_for(
    conn: Any,
    hyps: Sequence[Mapping[str, Any]],
    *,
    today: date | None = None,
) -> dict[str, dict[str, Any]]:
    """``{hypothesis_id: {"settles_on": iso | None, "settles_basis": {...}}}`` for every row."""
    today = today or _new_york_today()
    out: dict[str, dict[str, Any]] = {}
    waiting: list[tuple[str, str, str, str]] = []  # (hid, candidate_id, objective_id, run_id)

    for hyp in hyps:
        hid = str(hyp.get("id"))
        closed = _closed_reason(hyp)
        if closed is not None:
            out[hid] = {"settles_on": None, "settles_basis": closed}
            continue
        cid, oid, rid = lineage_refs(hyp)
        if not cid:
            out[hid] = {"settles_on": None, "settles_basis": {"reason": REASON_NO_LINEAGE}}
            continue
        waiting.append((hid, cid, oid, rid))
    if not waiting:
        return out

    # Objective through the run, for lineage written before origin_ref carried it.
    run_objective: dict[str, str] = {}
    runs = _read(
        conn,
        "objective_run",
        f"SELECT id, objective_id FROM {TABLE_RESEARCH_OBJECTIVE_RUN} WHERE id = ANY(%s)",
        (rid for _h, _c, oid, rid in waiting if not oid and rid),
    )
    for row in runs or []:
        run_objective[str(_cell(row, 0, "id"))] = str(_cell(row, 1, "objective_id") or "").strip()
    objective_of = {hid: oid or run_objective.get(rid, "") for hid, _c, oid, rid in waiting}

    # A failed objective read costs the rule, not the date: defaults, as resolve_active does.
    objectives: dict[str, Mapping[str, Any]] = {}
    for row in _read(
        conn,
        "objective",
        f"SELECT id, policy_json FROM {TABLE_RESEARCH_OBJECTIVE} WHERE id = ANY(%s)",
        objective_of.values(),
    ) or []:
        policy = _cell(row, 1, "policy_json")
        if isinstance(policy, (str, bytes, bytearray)):
            try:
                policy = json.loads(policy)
            except ValueError:
                policy = None
        objectives[str(_cell(row, 0, "id"))] = {"policy_json": policy if isinstance(policy, Mapping) else None}

    candidates = _read(
        conn,
        "candidate_pool",
        f"SELECT id, trade_date FROM {TABLE_RESEARCH_CANDIDATE_POOL} WHERE id = ANY(%s)",
        (cid for _h, cid, _o, _r in waiting),
    )
    trade_dates = (
        None if candidates is None else {str(_cell(r, 0, "id")): _as_date(_cell(r, 1, "trade_date")) for r in candidates}
    )

    exits: dict[tuple[str, int], date] = {}
    for row in _read(
        conn,
        "candidate_outcome",
        f"""
        SELECT candidate_id, horizon_days, exit_date
        FROM {TABLE_RESEARCH_CANDIDATE_OUTCOME}
        WHERE candidate_id = ANY(%s) AND exit_date IS NOT NULL
        """,
        (cid for _h, cid, _o, _r in waiting),
    ) or []:
        exit_date = _as_date(_cell(row, 2, "exit_date"))
        if exit_date is not None:
            exits[(str(_cell(row, 0, "candidate_id")), int(_cell(row, 1, "horizon_days")))] = exit_date

    projections: list[tuple[str, date, int, dict[str, Any]]] = []
    for hid, cid, _oid, _rid in waiting:
        objective_id = objective_of.get(hid) or None
        objective = objectives.get(objective_id or "")
        policy = (objective or {}).get("policy_json")
        rule: ResolutionPolicy = rule_from_objective(objective)
        basis: dict[str, Any] = {
            "candidate_id": cid,
            "objective_id": objective_id,
            "horizon_sessions": rule.horizon_days,
            "source": SOURCE_POLICY if isinstance(policy, Mapping) and "resolution" in policy else SOURCE_DEFAULT,
        }
        if not rule.enabled:
            out[hid] = {"settles_on": None, "settles_basis": {"reason": REASON_RULE_DISABLED, **basis}}
            continue
        if rule.horizon_days not in DEFAULT_HORIZONS:
            # The engine writes only these horizons; the rule would wait forever.
            out[hid] = {
                "settles_on": None,
                "settles_basis": {"reason": REASON_HORIZON_NOT_SETTLED, "engine_horizons": list(DEFAULT_HORIZONS), **basis},
            }
            continue
        if trade_dates is None:
            out[hid] = {"settles_on": None, "settles_basis": {"reason": REASON_LOOKUP_FAILED, **basis}}
            continue
        if cid not in trade_dates:
            out[hid] = {"settles_on": None, "settles_basis": {"reason": REASON_CANDIDATE_MISSING, **basis}}
            continue
        trade_date = trade_dates[cid]
        if trade_date is None:
            out[hid] = {"settles_on": None, "settles_basis": {"reason": REASON_NO_TRADE_DATE, **basis}}
            continue
        basis["trade_date"] = trade_date.isoformat()
        settled = exits.get((cid, rule.horizon_days))
        if settled is not None:
            out[hid] = {
                "settles_on": settled.isoformat(),
                "settles_basis": {"from": FROM_OUTCOME, "settled": True, "reason": None, **basis},
            }
            continue
        if trade_date < today - timedelta(days=DEFAULT_LOOKBACK_DAYS):
            out[hid] = {
                "settles_on": None,
                "settles_basis": {"reason": REASON_OUTSIDE_WINDOW, "lookback_days": DEFAULT_LOOKBACK_DAYS, **basis},
            }
            continue
        projections.append((hid, trade_date, rule.horizon_days, basis))

    if projections:
        start = min(td for _h, td, _n, _b in projections)
        end = max(td for _h, td, _n, _b in projections) + timedelta(
            days=2 * max(n for _h, _td, n, _b in projections) + 14
        )
        closed_days = cached_closed_days(conn, start, end)
        for hid, trade_date, horizon, basis in projections:
            settles = settlement_session(trade_date, horizon, closed_days)
            entry: dict[str, Any] = {"from": FROM_TRADE_DATE, "settled": False, "reason": None, **basis}
            if settles < today:
                # Past its exit session with no outcome yet: the engine has no bar
                # for it (halted, delisted, late feed). Still the date it is owed.
                entry["overdue"] = True
            out[hid] = {"settles_on": settles.isoformat(), "settles_basis": entry}
    return out


def attach_settlement(conn: Any, rows: list[dict[str, Any]], *, today: date | None = None) -> list[dict[str, Any]]:
    """Add ``settles_on`` / ``settles_basis`` to hypothesis rows in place (fail-soft)."""
    if not rows:
        return rows
    try:
        found = settlements_for(conn, rows, today=today)
    except Exception as exc:  # noqa: BLE001
        logger.warning("settlement: derivation failed: %s", str(exc)[:200])
        rollback_quietly(conn)
        found = {}
    for row in rows:
        derived = found.get(str(row.get("id"))) or {"settles_on": None, "settles_basis": {"reason": REASON_LOOKUP_FAILED}}
        row["settles_on"] = derived["settles_on"]
        row["settles_basis"] = derived["settles_basis"]
    return rows


__all__ = ["attach_settlement", "settlements_for"]
