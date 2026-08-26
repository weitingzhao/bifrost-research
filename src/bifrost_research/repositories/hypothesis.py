"""SQL layer for ``research.hypothesis`` — Wave RS-A workflow object.

D-RS-a locked: table lives in Golden Source ``research`` schema. All rows are
soft-deletable via ``retired_at``.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from bifrost_research.schema.schemas import TABLE_RESEARCH_HYPOTHESIS

_ALLOWED_STATUSES = frozenset({"active", "validated", "rejected", "archived"})

_HYPOTHESIS_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "thesis",
    "symbols",
    "tags",
    "status",
    "origin_page",
    "origin_ref",
    "linked_opportunity_ids",
    "linked_backtest_ids",
    "conclusion",
    "created_at",
    "updated_at",
    "retired_at",
)

_JSON_COLS = frozenset({"origin_ref"})
_ARRAY_COLS = frozenset({"symbols", "tags", "linked_opportunity_ids", "linked_backtest_ids"})
_TS_COLS = frozenset({"created_at", "updated_at", "retired_at"})


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, *, max_len: int = 48) -> str:
    slug = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return (slug or "hypothesis")[:max_len]


def generate_hypothesis_id(title: str) -> str:
    """Deterministic-ish slug + monotonically increasing suffix.

    Uses ``secrets.token_hex(3)`` for uniqueness; slug is for humans only.
    """
    base = _slugify(title)
    ts = int(time.time() * 1000) & 0xFFFFFF
    return f"{base}-{ts:06x}{secrets.token_hex(2)}"


def _validate_status(status: str | None) -> str:
    if status is None or status == "":
        return "active"
    if status not in _ALLOWED_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    return status


def _prepare_string_array(value: Sequence[str] | None) -> list[str]:
    if not value:
        return []
    cleaned = [str(item).strip() for item in value if str(item).strip()]
    seen: set[str] = set()
    deduped: list[str] = []
    for item in cleaned:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _prepare_symbols(value: Sequence[str] | None) -> list[str]:
    if not value:
        return []
    upper = [str(item).strip().upper() for item in value if str(item).strip()]
    seen: set[str] = set()
    deduped: list[str] = []
    for item in upper:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _serialize_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _row_to_dict(row: Any, columns: Sequence[str] = _HYPOTHESIS_COLUMNS) -> dict[str, Any]:
    if isinstance(row, Mapping):
        out = {col: row[col] for col in columns if col in row}
    else:
        out = {columns[i]: row[i] for i in range(min(len(columns), len(row)))}
    for col in _JSON_COLS:
        val = out.get(col)
        if isinstance(val, (bytes, bytearray)):
            val = val.decode("utf-8", errors="replace")
        if isinstance(val, str):
            try:
                out[col] = json.loads(val)
            except Exception:
                out[col] = val
    for col in _ARRAY_COLS:
        val = out.get(col)
        if val is None:
            out[col] = []
        elif isinstance(val, tuple):
            out[col] = list(val)
    for col in _TS_COLS:
        if col in out:
            out[col] = _iso(out[col])
    return out


def _column_list() -> str:
    return ", ".join(_HYPOTHESIS_COLUMNS)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_hypothesis(
    conn: _Connection,
    *,
    title: str,
    thesis: str,
    symbols: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    status: str | None = "active",
    origin_page: str | None = None,
    origin_ref: Any = None,
    linked_opportunity_ids: Sequence[str] | None = None,
    linked_backtest_ids: Sequence[str] | None = None,
    conclusion: str | None = None,
    hypothesis_id: str | None = None,
) -> dict[str, Any]:
    if not title or not title.strip():
        raise ValueError("title is required")
    if not thesis or not thesis.strip():
        raise ValueError("thesis is required")
    validated_status = _validate_status(status)
    hid = (hypothesis_id or generate_hypothesis_id(title)).strip()
    if not hid:
        raise ValueError("hypothesis id is required")

    sql = f"""
        INSERT INTO {TABLE_RESEARCH_HYPOTHESIS} (
            id, title, thesis, symbols, tags, status,
            origin_page, origin_ref,
            linked_opportunity_ids, linked_backtest_ids,
            conclusion
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING {_column_list()}
    """
    params = (
        hid,
        title.strip(),
        thesis.strip(),
        _prepare_symbols(symbols),
        _prepare_string_array(tags),
        validated_status,
        (origin_page or None),
        _serialize_json(origin_ref),
        _prepare_string_array(linked_opportunity_ids),
        _prepare_string_array(linked_backtest_ids),
        (conclusion or None),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row is None:
        raise RuntimeError("insert returned no row")
    return _row_to_dict(row)


def get_hypothesis(conn: _Connection, hypothesis_id: str) -> dict[str, Any] | None:
    sql = f"""
        SELECT {_column_list()}
        FROM {TABLE_RESEARCH_HYPOTHESIS}
        WHERE id = %s
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (hypothesis_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_hypotheses(
    conn: _Connection,
    *,
    status: str | None = None,
    symbol: str | None = None,
    tag: str | None = None,
    include_retired: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if not include_retired:
        clauses.append("retired_at IS NULL")
    if status:
        _validate_status(status)
        clauses.append("status = %s")
        params.append(status)
    if symbol:
        clauses.append("%s = ANY(symbols)")
        params.append(symbol.strip().upper())
    if tag:
        clauses.append("%s = ANY(tags)")
        params.append(tag.strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT {_column_list()}
        FROM {TABLE_RESEARCH_HYPOTHESIS}
        {where}
        ORDER BY updated_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([int(limit), int(offset)])
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        raw = cur.fetchall() or []
    return [_row_to_dict(r) for r in raw]


def patch_hypothesis(
    conn: _Connection,
    hypothesis_id: str,
    fields: Mapping[str, Any],
) -> dict[str, Any] | None:
    allowed = {
        "title",
        "thesis",
        "symbols",
        "tags",
        "status",
        "origin_page",
        "origin_ref",
        "linked_opportunity_ids",
        "linked_backtest_ids",
        "conclusion",
    }
    updates: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "status":
            updates[key] = _validate_status(value)
        elif key == "symbols":
            updates[key] = _prepare_symbols(value)
        elif key in {"tags", "linked_opportunity_ids", "linked_backtest_ids"}:
            updates[key] = _prepare_string_array(value)
        elif key == "origin_ref":
            updates[key] = _serialize_json(value)
        elif key in {"title", "thesis"}:
            if not value or not str(value).strip():
                raise ValueError(f"{key} cannot be empty")
            updates[key] = str(value).strip()
        else:
            updates[key] = value if value in (None, "") else str(value)
    if not updates:
        return get_hypothesis(conn, hypothesis_id)

    assignments = ", ".join(f"{col} = %s" for col in updates)
    params = list(updates.values())
    sql = f"""
        UPDATE {TABLE_RESEARCH_HYPOTHESIS}
        SET {assignments}, updated_at = now()
        WHERE id = %s
        RETURNING {_column_list()}
    """
    params.append(hypothesis_id)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row is None:
        return None
    return _row_to_dict(row)


def retire_hypothesis(conn: _Connection, hypothesis_id: str) -> dict[str, Any] | None:
    sql = f"""
        UPDATE {TABLE_RESEARCH_HYPOTHESIS}
        SET retired_at = now(), status = 'archived', updated_at = now()
        WHERE id = %s AND retired_at IS NULL
        RETURNING {_column_list()}
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (hypothesis_id,))
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row is None:
        return None
    return _row_to_dict(row)


def active_summary(conn: _Connection, *, top_n: int = 5) -> dict[str, Any]:
    """Counts per status + top-N most-recently-updated active hypotheses."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT status, COUNT(*)
            FROM {TABLE_RESEARCH_HYPOTHESIS}
            WHERE retired_at IS NULL
            GROUP BY status
            """
        )
        rows = cur.fetchall() or []
    counts: dict[str, int] = {s: 0 for s in _ALLOWED_STATUSES}
    for row in rows:
        if isinstance(row, Mapping):
            key = str(row.get("status") or "")
            value = row.get("count") or 0
        else:
            key = str(row[0] or "")
            value = row[1] or 0
        if key in counts:
            counts[key] = int(value)
    top = list_hypotheses(conn, status="active", limit=int(top_n))
    return {
        "counts": counts,
        "total_active": counts.get("active", 0),
        "recent_active": top,
    }
