"""Shared upsert helper for market_analytics / research schema writes."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def batch_upsert(
    conn: _Connection,
    table: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    conflict_keys: Sequence[str],
    update_cols: Sequence[str] | None = None,
    set_fetched_at: bool = True,
    auto_commit: bool = True,
) -> int:
    """Insert rows with ``ON CONFLICT DO UPDATE``. Returns number of input rows.

    ``table`` must be a qualified name like ``features_daily.max_pain_daily``.
    """
    if not rows:
        return 0
    cols = list(columns)
    conflict = list(conflict_keys)
    if not cols or not conflict:
        raise ValueError("columns and conflict_keys are required")

    updates = list(update_cols) if update_cols is not None else [c for c in cols if c not in conflict]
    set_parts = [f"{c} = EXCLUDED.{c}" for c in updates]
    if set_fetched_at and "fetched_at" not in updates and "fetched_at" not in conflict:
        if "fetched_at" in cols or set_fetched_at:
            set_parts.append("fetched_at = now()")

    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(cols)
    conflict_list = ", ".join(conflict)
    if set_parts:
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_list}) DO UPDATE SET {', '.join(set_parts)}"
        )
    else:
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_list}) DO NOTHING"
        )

    prepared: list[tuple[Any, ...]] = []
    for row in rows:
        if len(row) != len(cols):
            raise ValueError(f"row length {len(row)} != columns {len(cols)}")
        prepared.append(tuple(_prepare_value(v) for v in row))

    try:
        with conn.cursor() as cur:
            cur.executemany(sql, prepared)
        if auto_commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(prepared)


def _prepare_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value
