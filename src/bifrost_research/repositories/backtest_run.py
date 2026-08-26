"""SQL layer for ``research.backtest_run`` — Wave RS-C4.

Persists results of event-driven backtest runs, optionally linked to a
``research.hypothesis`` row. When ``hypothesis_id`` is set, callers should
also PATCH ``hypothesis.linked_backtest_ids`` — see ``api/backtest_event.py``.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Protocol, Sequence

from bifrost_research.schema.schemas import (
    TABLE_RESEARCH_BACKTEST_RUN,
    TABLE_RESEARCH_HYPOTHESIS,
)


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


_COLUMNS: tuple[str, ...] = (
    "id",
    "hypothesis_id",
    "event_def",
    "strategy_template",
    "fill_config",
    "lookback_years",
    "summary",
    "walk_forward",
    "benchmark",
    "created_at",
)

_JSON_COLS = frozenset({"event_def", "fill_config", "summary", "walk_forward", "benchmark"})
_TS_COLS = frozenset({"created_at"})


def generate_run_id() -> str:
    ts = int(time.time() * 1000) & 0xFFFFFFFF
    return f"bt-{ts:08x}{secrets.token_hex(3)}"


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
    if isinstance(dt, date):
        return dt.isoformat()
    return str(dt)


def _row_to_dict(row: Any, columns: Sequence[str] = _COLUMNS) -> dict[str, Any]:
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
    for col in _TS_COLS:
        if col in out:
            out[col] = _iso(out[col])
    return out


def _cols() -> str:
    return ", ".join(_COLUMNS)


def create_run(
    conn: _Connection,
    *,
    event_def: Mapping[str, Any],
    strategy_template: str,
    fill_config: Mapping[str, Any],
    lookback_years: int,
    summary: Mapping[str, Any],
    walk_forward: Any = None,
    benchmark: Any = None,
    hypothesis_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if not strategy_template or not strategy_template.strip():
        raise ValueError("strategy_template is required")
    rid = (run_id or generate_run_id()).strip()
    if not rid:
        raise ValueError("run_id is required")

    sql = f"""
        INSERT INTO {TABLE_RESEARCH_BACKTEST_RUN} (
            id, hypothesis_id, event_def, strategy_template, fill_config,
            lookback_years, summary, walk_forward, benchmark
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING {_cols()}
    """
    params = (
        rid,
        (hypothesis_id or None),
        _serialize_json(dict(event_def)),
        strategy_template.strip(),
        _serialize_json(dict(fill_config or {})),
        int(lookback_years),
        _serialize_json(dict(summary or {})),
        _serialize_json(walk_forward) if walk_forward is not None else None,
        _serialize_json(benchmark) if benchmark is not None else None,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row is None:  # pragma: no cover
        raise RuntimeError("backtest_run insert returned no row")
    return _row_to_dict(row)


def get_run(conn: _Connection, run_id: str) -> dict[str, Any] | None:
    sql = f"""
        SELECT {_cols()}
        FROM {TABLE_RESEARCH_BACKTEST_RUN}
        WHERE id = %s
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (run_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_runs(
    conn: _Connection,
    *,
    hypothesis_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if hypothesis_id:
        clauses.append("hypothesis_id = %s")
        params.append(hypothesis_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT {_cols()}
        FROM {TABLE_RESEARCH_BACKTEST_RUN}
        {where}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([int(limit), int(offset)])
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        raw = cur.fetchall() or []
    return [_row_to_dict(r) for r in raw]


def append_to_hypothesis(
    conn: _Connection,
    hypothesis_id: str,
    run_id: str,
) -> bool:
    """Append ``run_id`` to ``linked_backtest_ids`` on the hypothesis row.

    Idempotent: uses array uniqueness. Returns True on success.
    """
    sql = f"""
        UPDATE {TABLE_RESEARCH_HYPOTHESIS}
        SET linked_backtest_ids = (
                SELECT ARRAY(SELECT DISTINCT UNNEST(linked_backtest_ids || ARRAY[%s]::text[]))
            ),
            updated_at = now()
        WHERE id = %s
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (run_id, hypothesis_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


__all__ = [
    "create_run",
    "get_run",
    "list_runs",
    "append_to_hypothesis",
    "generate_run_id",
]
