"""SQL layer for ``research.candidate_pool`` — Wave Loop v1."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from bifrost_research.schema.schemas import TABLE_RESEARCH_CANDIDATE_POOL

_ALLOWED_STATUSES = frozenset({"open", "promoted", "dismissed", "expired"})
_ALLOWED_SOURCES = frozenset(
    {"scan", "screener", "event_radar", "momentum", "sepa", "copilot", "manual", "harness"}
)

_COLUMNS: tuple[str, ...] = (
    "id",
    "trade_date",
    "symbol",
    "source",
    "source_ref",
    "score",
    "lens_snapshot",
    "tags",
    "status",
    "hypothesis_id",
    "owner_id",
    "created_at",
    "ttl_at",
)

_JSON_COLS = frozenset({"source_ref", "lens_snapshot"})
_ARRAY_COLS = frozenset({"tags"})
_TS_COLS = frozenset({"created_at", "ttl_at"})
_DATE_COLS = frozenset({"trade_date"})


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def generate_candidate_id(symbol: str) -> str:
    sym = (symbol or "SYM").strip().upper()[:12] or "SYM"
    ts = int(time.time() * 1000) & 0xFFFFFF
    return f"cand-{sym.lower()}-{ts:06x}{secrets.token_hex(2)}"


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


def _row_to_dict(row: Sequence[Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for i, col in enumerate(_COLUMNS):
        val = row[i]
        if col in _JSON_COLS:
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except json.JSONDecodeError:
                    pass
            elif val is None and col == "lens_snapshot":
                val = {}
        elif col in _ARRAY_COLS:
            val = list(val) if val is not None else []
        elif col in _TS_COLS or col in _DATE_COLS:
            val = _iso(val)
        elif col == "score" and val is not None:
            val = float(val)
        out[col] = val
    return out


def create_candidate(
    conn: _Connection,
    *,
    symbol: str,
    source: str,
    trade_date: date | None = None,
    source_ref: Mapping[str, Any] | None = None,
    score: float | None = None,
    lens_snapshot: Mapping[str, Any] | None = None,
    tags: Sequence[str] | None = None,
    owner_id: str = "owner",
    ttl_days: int = 5,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol is required")
    src = (source or "").strip().lower()
    if src not in _ALLOWED_SOURCES:
        raise ValueError(f"invalid source: {source!r}")

    cid = candidate_id or generate_candidate_id(sym)
    td = trade_date or date.today()
    ttl_at = datetime.now(timezone.utc) + timedelta(days=max(1, ttl_days))
    tag_list = [t.strip() for t in (tags or []) if t and str(t).strip()]

    sql = f"""
        INSERT INTO {TABLE_RESEARCH_CANDIDATE_POOL} (
            id, trade_date, symbol, source, source_ref, score,
            lens_snapshot, tags, status, owner_id, ttl_at
        ) VALUES (
            %s, %s, %s, %s, %s::jsonb, %s,
            %s::jsonb, %s, 'open', %s, %s
        )
        RETURNING {", ".join(_COLUMNS)}
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                cid,
                td,
                sym,
                src,
                _serialize_json(dict(source_ref or {})),
                score,
                _serialize_json(dict(lens_snapshot or {})),
                tag_list,
                owner_id,
                ttl_at,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    result = _row_to_dict(row)
    assert result is not None
    return result


def list_candidates(
    conn: _Connection,
    *,
    status: str | None = "open",
    source: str | None = None,
    symbol: str | None = None,
    owner_id: str | None = None,
    days: int | None = 30,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        if status not in _ALLOWED_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        clauses.append("status = %s")
        params.append(status)
    if source:
        clauses.append("source = %s")
        params.append(source.strip().lower())
    if symbol:
        clauses.append("symbol = %s")
        params.append(symbol.strip().upper())
    if owner_id:
        clauses.append("owner_id = %s")
        params.append(owner_id)
    if days is not None and days > 0:
        clauses.append("trade_date >= (CURRENT_DATE - %s::int)")
        params.append(days)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT {", ".join(_COLUMNS)}
        FROM {TABLE_RESEARCH_CANDIDATE_POOL}
        {where}
        ORDER BY trade_date DESC, created_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows if r]  # type: ignore[misc]


def get_candidate(conn: _Connection, candidate_id: str) -> dict[str, Any] | None:
    sql = f"""
        SELECT {", ".join(_COLUMNS)}
        FROM {TABLE_RESEARCH_CANDIDATE_POOL}
        WHERE id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (candidate_id,))
        row = cur.fetchone()
    return _row_to_dict(row)


def dismiss_candidate(conn: _Connection, candidate_id: str) -> dict[str, Any] | None:
    sql = f"""
        UPDATE {TABLE_RESEARCH_CANDIDATE_POOL}
        SET status = 'dismissed'
        WHERE id = %s AND status = 'open'
        RETURNING {", ".join(_COLUMNS)}
    """
    with conn.cursor() as cur:
        cur.execute(sql, (candidate_id,))
        row = cur.fetchone()
    conn.commit()
    return _row_to_dict(row)


def promote_candidate(
    conn: _Connection,
    candidate_id: str,
    *,
    hypothesis_id: str,
) -> dict[str, Any] | None:
    sql = f"""
        UPDATE {TABLE_RESEARCH_CANDIDATE_POOL}
        SET status = 'promoted', hypothesis_id = %s
        WHERE id = %s AND status = 'open'
        RETURNING {", ".join(_COLUMNS)}
    """
    with conn.cursor() as cur:
        cur.execute(sql, (hypothesis_id, candidate_id))
        row = cur.fetchone()
    conn.commit()
    return _row_to_dict(row)


def expire_stale(conn: _Connection) -> int:
    """Mark open candidates past ttl_at as expired. Returns row count."""
    sql = f"""
        UPDATE {TABLE_RESEARCH_CANDIDATE_POOL}
        SET status = 'expired'
        WHERE status = 'open' AND ttl_at IS NOT NULL AND ttl_at < now()
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        n = cur.rowcount or 0
    conn.commit()
    return int(n)
