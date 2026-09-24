"""SQL layer for ``research.saved_screen`` — 6A saved-screen store.

A screen is one object with one id. The authoring face writes it, Trade's
result face renders the same object read-only, so the filter vocabulary is
defined here and nowhere else: ``definition`` is validated against the
condition catalog below, strictly — an unknown filter key or condition name
is an error, not a passthrough, because a definition Trade cannot render is
worse than a rejected save.

``vocabulary`` stamps which catalog the definition speaks. When the dbt
vocabulary changes, add a catalog under a new stamp; existing rows keep
theirs and stay renderable.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from bifrost_research.schema.schemas import TABLE_RESEARCH_SAVED_SCREEN

VOCABULARY_V1 = "sepa_screener_wide.v1"

# mart_sepa_technical_eval / mart_sepa_fundamental_eval condition columns.
TECH_CONDITIONS_V1 = frozenset(
    {
        "price_gt_sma200",
        "sma150_gt_sma200",
        "price_gt_sma150",
        "sma50_gt_sma200",
        "avg_volume_50_gt_threshold",
        "close_ge_low52_x_1_3",
        "sma50_gt_sma150",
        "price_gt_sma50",
        "sma200_rising_1m",
        "close_ge_high52_x_0_75",
        "crs_ge_70",
    }
)
FUND_CONDITIONS_V1 = frozenset(
    {
        "eps_q2q_ge_25pct",
        "rev_q2q_ge_25pct",
        "eps_acc_2q",
        "rev_acc_2q",
        "eps_3y_ge_15pct",
        "rev_3y_ge_15pct",
        "eps_acc_fy",
        "rev_acc_fy",
    }
)
PATHS_V1 = frozenset({"PIVOT", "SETUP", "WATCH", "AVOID"})
GRADES_V1 = frozenset({"A+", "A", "B", "C", "D"})

_DEFINITION_KEYS = frozenset({"q", "paths", "grades", "min_composite", "tech", "fund"})

_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "description",
    "definition",
    "vocabulary",
    "is_active",
    "origin_page",
    "created_at",
    "updated_at",
    "retired_at",
)

_SELECT_COLS = ", ".join(_COLUMNS)


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def generate_screen_id(name: str) -> str:
    slug = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")[:48] or "screen"
    ts = int(time.time() * 1000) & 0xFFFFFF
    return f"scr-{slug}-{ts:06x}{secrets.token_hex(2)}"


def _subset_of(value: Any, allowed: frozenset[str], what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"definition.{what} must be a list")
    items = [str(v) for v in value]
    unknown = [v for v in items if v not in allowed]
    if unknown:
        raise ValueError(f"unknown {what}: {', '.join(sorted(set(unknown)))}")
    # keep order, drop duplicates
    seen: set[str] = set()
    return [v for v in items if not (v in seen or seen.add(v))]


def normalize_definition(raw: Any) -> dict[str, Any]:
    """Validate strictly against the v1 vocabulary; raise ValueError on drift."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, Mapping):
        raise ValueError("definition must be an object")
    unknown = set(raw.keys()) - _DEFINITION_KEYS
    if unknown:
        raise ValueError(f"unknown definition keys: {', '.join(sorted(unknown))}")
    min_composite = raw.get("min_composite", 0)
    if not isinstance(min_composite, (int, float)) or not 0 <= float(min_composite) <= 100:
        raise ValueError("definition.min_composite must be a number in 0–100")
    q = raw.get("q", "")
    if not isinstance(q, str):
        raise ValueError("definition.q must be a string")
    return {
        "q": q.strip(),
        "paths": _subset_of(raw.get("paths"), PATHS_V1, "paths"),
        "grades": _subset_of(raw.get("grades"), GRADES_V1, "grades"),
        "min_composite": float(min_composite),
        "tech": _subset_of(raw.get("tech"), TECH_CONDITIONS_V1, "tech"),
        "fund": _subset_of(raw.get("fund"), FUND_CONDITIONS_V1, "fund"),
    }


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        out = {col: row[col] for col in _COLUMNS if col in row}
    else:
        out = {_COLUMNS[i]: row[i] for i in range(min(len(_COLUMNS), len(row)))}
    val = out.get("definition")
    if isinstance(val, (bytes, bytearray)):
        val = val.decode("utf-8", errors="replace")
    if isinstance(val, str):
        try:
            out["definition"] = json.loads(val)
        except Exception:
            pass
    for col in ("created_at", "updated_at", "retired_at"):
        if col in out:
            out[col] = _iso(out[col])
    return out


def create_screen(
    conn: _Connection,
    *,
    name: str,
    definition: Any,
    description: str | None = None,
    origin_page: str | None = None,
) -> dict[str, Any]:
    clean = normalize_definition(definition)
    screen_id = generate_screen_id(name)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_RESEARCH_SAVED_SCREEN}
                (id, name, description, definition, vocabulary, origin_page)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING {_SELECT_COLS}
            """,
            (screen_id, name, description, json.dumps(clean), VOCABULARY_V1, origin_page),
        )
        row = cur.fetchone()
    conn.commit()
    return _row_to_dict(row)


def list_screens(conn: _Connection, *, include_retired: bool = False) -> list[dict[str, Any]]:
    where = "" if include_retired else "WHERE retired_at IS NULL"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_SELECT_COLS} FROM {TABLE_RESEARCH_SAVED_SCREEN}
            {where}
            ORDER BY updated_at DESC
            """
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def get_screen(conn: _Connection, screen_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM {TABLE_RESEARCH_SAVED_SCREEN} WHERE id = %s",
            (screen_id,),
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def patch_screen(conn: _Connection, screen_id: str, updates: Mapping[str, Any]) -> dict[str, Any] | None:
    """Update name / description / definition / is_active; bumps updated_at."""
    sets: list[str] = []
    params: list[Any] = []
    for key in ("name", "description", "origin_page"):
        if key in updates:
            sets.append(f"{key} = %s")
            params.append(updates[key])
    if "definition" in updates:
        sets.append("definition = %s")
        params.append(json.dumps(normalize_definition(updates["definition"])))
    if "is_active" in updates:
        sets.append("is_active = %s")
        params.append(bool(updates["is_active"]))
    if not sets:
        return get_screen(conn, screen_id)
    sets.append("updated_at = now()")
    params.append(screen_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_RESEARCH_SAVED_SCREEN} SET {", ".join(sets)}
            WHERE id = %s
            RETURNING {_SELECT_COLS}
            """,
            tuple(params),
        )
        row = cur.fetchone()
    conn.commit()
    return _row_to_dict(row) if row else None


def retire_screen(conn: _Connection, screen_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_RESEARCH_SAVED_SCREEN}
            SET retired_at = now(), is_active = false, updated_at = now()
            WHERE id = %s AND retired_at IS NULL
            RETURNING {_SELECT_COLS}
            """,
            (screen_id,),
        )
        row = cur.fetchone()
    conn.commit()
    return _row_to_dict(row) if row else None
