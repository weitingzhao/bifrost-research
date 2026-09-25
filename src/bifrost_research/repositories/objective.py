"""SQL layer for ``research.objective`` — Wave A Harness. Runs live in ``objective_run.py``."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping, Sequence
from typing import Any

from bifrost_research.repositories.objective_common import Connection as _Connection
from bifrost_research.repositories.objective_common import _iso, _serialize_json
from bifrost_research.repositories.objective_run import (  # noqa: F401 — re-exported: callers use obj_repo.<run fn>
    _ALLOWED_RUN_STATUSES,
    _RUN_COLS,
    _run_row,
    append_run_trace_event,
    count_candidates_for_run,
    create_run,
    delete_run,
    finish_run,
    force_delete_run,
    generate_run_id,
    get_run,
    list_runs,
    patch_run_outputs,
    patch_run_trace,
    replace_run_plan,
    update_run_status,
)
from bifrost_research.schema.schemas import (
    TABLE_RESEARCH_OBJECTIVE,
    TABLE_RESEARCH_OBJECTIVE_RUN,
)

_ALLOWED_SCHEDULES = frozenset({"daily_open", "daily_eod", "weekly", "adhoc"})
# The whole vocabulary: the console lists `active`, everything else is out of
# sight. `paused` and `retired` were named here at the start, never written by
# anything, and never checked — the constant had no readers, so the word the
# system actually uses, `archived`, contradicted it in silence for months.
# Enforced below, because an unrecognised status is invisible twice over: gone
# from the console's active list and absent from `?status=archived`.
OBJECTIVE_STATUSES = frozenset({"active", "archived"})
# Who produces the candidates (Trade design Rev .55): the operator by hand, the
# loop with the operator deciding, or the loop with the leash deciding. The
# database CHECK holds the same three words.
OBJECTIVE_MODES = frozenset({"hand", "assisted", "auto"})

_OBJ_COLS: tuple[str, ...] = (
    "id",
    "title",
    "description",
    "schedule",
    "policy_json",
    "persona",
    "status",
    "owner_id",
    "created_at",
    "mode",
    "subject",
)



def _mode(value: str) -> str:
    mode = (value or "").strip().lower()
    if mode not in OBJECTIVE_MODES:
        raise ValueError(f"invalid mode: {value!r} (one of {', '.join(sorted(OBJECTIVE_MODES))})")
    return mode


def _subject(value: str | None) -> str | None:
    """A ticker as the rest of the system stores it; blank means none."""
    sym = (value or "").strip().upper()
    return sym or None


def generate_objective_id(title: str) -> str:
    base = "".join(c if c.isalnum() else "-" for c in (title or "obj").lower())[:32].strip("-")
    ts = int(time.time() * 1000) & 0xFFFFFF
    return f"obj-{base or 'obj'}-{ts:06x}{secrets.token_hex(2)}"


def _obj_row(row: Sequence[Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for i, col in enumerate(_OBJ_COLS):
        val = row[i]
        if col == "policy_json":
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except json.JSONDecodeError:
                    val = {}
            elif val is None:
                val = {}
        elif col == "created_at":
            val = _iso(val)
        out[col] = val
    return out


def create_objective(
    conn: _Connection,
    *,
    title: str,
    description: str,
    schedule: str = "adhoc",
    policy_json: Mapping[str, Any] | None = None,
    persona: str = "loop_curator",
    owner_id: str = "owner",
    objective_id: str | None = None,
    mode: str = "assisted",
    subject: str | None = None,
) -> dict[str, Any]:
    sched = (schedule or "adhoc").strip().lower()
    if sched not in _ALLOWED_SCHEDULES:
        raise ValueError(f"invalid schedule: {schedule!r}")
    obj_mode = _mode(mode)
    oid = objective_id or generate_objective_id(title)
    sql = f"""
        INSERT INTO {TABLE_RESEARCH_OBJECTIVE} (
            id, title, description, schedule, policy_json, persona, status, owner_id, mode, subject
        ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, 'active', %s, %s, %s)
        RETURNING {", ".join(_OBJ_COLS)}
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                oid,
                title.strip(),
                description.strip(),
                sched,
                _serialize_json(dict(policy_json or {})),
                persona,
                owner_id,
                obj_mode,
                _subject(subject),
            ),
        )
        row = cur.fetchone()
    conn.commit()
    result = _obj_row(row)
    assert result is not None
    return result


def get_objective(conn: _Connection, objective_id: str) -> dict[str, Any] | None:
    sql = f"SELECT {', '.join(_OBJ_COLS)} FROM {TABLE_RESEARCH_OBJECTIVE} WHERE id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (objective_id,))
        return _obj_row(cur.fetchone())


POLICY_SUGGESTION_WHITELIST: frozenset[str] = frozenset(
    {
        "preset",
        "flag_filter",
        "min_composite_score",
        "min_hit_rate",
        "max_candidates",
        "universe_mode",
        "layers",
        "option_overlay",
        # B3: the outcome rule that resolves candidate-born hypotheses.
        "resolution",
        # D3: the leash — the source hit-rate floor an unattended accept needs.
        "min_source_hit_rate",
        # Both are real LoopPolicy fields the runtime honours (policy_schema.py:89
        # and :102). They reached plan_llm's suggestion whitelist without reaching
        # this one, so the model could propose them, the Inbox would show the card,
        # and approving it would silently drop them — a suggestion that changes
        # nothing, which is the failure the "0 fields to merge" work exists to
        # surface. test_policy_suggestion_contract keeps the two sets in step.
        "require_validate_pass",
        "discovery_assist",
    }
)

#: What the Owner may change from the objective page, over and above what a
#: model may propose. The base set is the model's — a suggestion that could
#: switch its own planner or judges off is not one a model should be able to
#: make — and the Owner's set adds the knobs that decide how a run is judged,
#: planned and seeded. Both routes still go through a draft, so the audit
#: trail reads the same whoever moved the knob.
OWNER_POLICY_WHITELIST: frozenset[str] = POLICY_SUGGESTION_WHITELIST | frozenset(
    # `decline_memory` is Owner-only on purpose: it encodes the Owner's own
    # refusals, and a model that could propose loosening it could propose
    # undoing them.
    {"triage", "persona_evaluate", "use_llm_plan", "llm_model", "seed_symbols", "decline_memory"}
)

_NESTED_POLICY_KEYS = frozenset(
    {"layers", "option_overlay", "discovery_assist", "resolution", "triage", "decline_memory"}
)


def _deep_merge_policy_patch(
    current: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Merge whitelist-filtered patch onto current policy (nested layers/overlay)."""
    merged = dict(current)
    for key, value in patch.items():
        if key in _NESTED_POLICY_KEYS and isinstance(value, dict):
            base_nested = merged.get(key)
            if not isinstance(base_nested, dict):
                base_nested = {}
            nested_out = dict(base_nested)
            for sub_key, sub_val in value.items():
                if isinstance(sub_val, dict) and isinstance(nested_out.get(sub_key), dict):
                    nested_out[sub_key] = {**nested_out[sub_key], **sub_val}
                else:
                    nested_out[sub_key] = sub_val
            merged[key] = nested_out
        else:
            merged[key] = value
    return merged


def patch_policy_json(
    conn: _Connection,
    objective_id: str,
    patch: Mapping[str, Any],
    *,
    whitelist: frozenset[str] | None = POLICY_SUGGESTION_WHITELIST,
) -> dict[str, Any] | None:
    """Merge ``patch`` (whitelist-filtered) onto ``objective.policy_json``.

    Wave Y.3 + LS-1: nested ``layers`` / ``option_overlay`` merge deeply.
    """
    if not isinstance(patch, Mapping):
        raise TypeError("patch must be a mapping")
    filtered: dict[str, Any] = {}
    if whitelist is None:
        filtered.update(dict(patch))
    else:
        for k, v in patch.items():
            if k in whitelist:
                filtered[k] = v
    if not filtered:
        return get_objective(conn, objective_id)

    current = get_objective(conn, objective_id)
    if current is None:
        return None
    current_policy = current.get("policy_json") or {}
    if not isinstance(current_policy, dict):
        current_policy = {}
    merged_policy = _deep_merge_policy_patch(current_policy, filtered)

    sql = f"""
        UPDATE {TABLE_RESEARCH_OBJECTIVE}
        SET policy_json = %s::jsonb
        WHERE id = %s
        RETURNING {", ".join(_OBJ_COLS)}
    """
    with conn.cursor() as cur:
        cur.execute(sql, (_serialize_json(merged_policy), objective_id))
        row = cur.fetchone()
    conn.commit()
    return _obj_row(row)


def list_objectives(
    conn: _Connection,
    *,
    status: str | None = "active",
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT {", ".join(_OBJ_COLS)}
        FROM {TABLE_RESEARCH_OBJECTIVE}
        {where}
        ORDER BY created_at DESC
        LIMIT %s
    """
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [_obj_row(r) for r in cur.fetchall() if r]  # type: ignore[misc]


ARCHIVED_STATUS = "archived"


def count_runs(conn: _Connection, objective_id: str) -> int:
    """How much history an objective carries — the cost of deleting it."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM {TABLE_RESEARCH_OBJECTIVE_RUN} WHERE objective_id = %s",
            (objective_id,),
        )
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def update_objective(
    conn: _Connection,
    objective_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    schedule: str | None = None,
    persona: str | None = None,
    mode: str | None = None,
    subject: str | None = None,
) -> dict[str, Any] | None:
    """Change what an objective is called, says, when it runs, and who works it.

    The policy is deliberately not here: it moves through a draft so the
    change carries a rationale and lands in the same ledger a model's would.
    These fields carry no strategy, so they are edited in place. `mode` is
    declared intent — `auto` still accepts nothing the leash's gate refuses.
    An empty `subject` clears it.
    """
    sets: list[str] = []
    params: list[Any] = []
    if title is not None and title.strip():
        sets.append("title = %s")
        params.append(title.strip())
    if description is not None:
        sets.append("description = %s")
        params.append(description.strip())
    if schedule is not None:
        sched = schedule.strip().lower()
        if sched not in _ALLOWED_SCHEDULES:
            raise ValueError(f"invalid schedule: {schedule!r}")
        sets.append("schedule = %s")
        params.append(sched)
    if persona is not None and persona.strip():
        sets.append("persona = %s")
        params.append(persona.strip())
    if mode is not None:
        sets.append("mode = %s")
        params.append(_mode(mode))
    if subject is not None:
        sets.append("subject = %s")
        params.append(_subject(subject))
    if not sets:
        return get_objective(conn, objective_id)
    params.append(objective_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_RESEARCH_OBJECTIVE}
            SET {", ".join(sets)}
            WHERE id = %s
            RETURNING {", ".join(_OBJ_COLS)}
            """,
            tuple(params),
        )
        row = cur.fetchone()
    conn.commit()
    return _obj_row(row) if row else None


def set_objective_status(
    conn: _Connection, objective_id: str, *, status: str
) -> dict[str, Any] | None:
    """Archive or reactivate. Archiving is how an objective leaves the console.

    Runs and the candidate lineage that points at them survive: the console
    lists `status = 'active'`, so archiving is enough to retire an objective
    without destroying what it produced.

    Raises ValueError on a status outside `OBJECTIVE_STATUSES`.
    """
    if status not in OBJECTIVE_STATUSES:
        raise ValueError(
            f"invalid objective status: {status!r} "
            f"(expected one of {sorted(OBJECTIVE_STATUSES)})"
        )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_RESEARCH_OBJECTIVE}
            SET status = %s
            WHERE id = %s
            RETURNING {", ".join(_OBJ_COLS)}
            """,
            (status, objective_id),
        )
        row = cur.fetchone()
    conn.commit()
    return _obj_row(row)


def delete_objective(conn: _Connection, objective_id: str) -> bool:
    """Remove an objective that never ran. Returns False when it does not exist.

    Only safe with no runs, and the caller is expected to have checked: the
    foreign key from objective_run has no ON DELETE clause, so Postgres refuses
    the delete anyway — this just makes the refusal a decision rather than an
    integrity error surfacing as a 500.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {TABLE_RESEARCH_OBJECTIVE} WHERE id = %s",
            (objective_id,),
        )
        deleted = cur.rowcount
    conn.commit()
    return bool(deleted)
