"""Reads and appends for the suggestion ledger. Nothing here updates or deletes."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Iterable

from bifrost_research.engines.suggestion.contract import Suggestion
from bifrost_research.schema.schemas import SCHEMA_RESEARCH, TABLE_STOCK_SIGNAL_PINE_DAILY

T_SUGGESTION = f"{SCHEMA_RESEARCH}.suggestion"
T_SETTLEMENT = f"{SCHEMA_RESEARCH}.suggestion_settlement"

_SUGGESTION_COLS = (
    "suggestion_id",
    "issue_key",
    "as_of_session",
    "source",
    "source_ref",
    "source_version",
    "symbol",
    "kind",
    "structure",
    "legs_json",
    "take_profit_pct",
    "stop_loss_mult",
    "exit_dte",
    "max_hold_days",
    "horizon_days",
    "expected_credit",
    "max_loss",
    "pop",
    "expected_pnl",
    "conviction",
    "snapshot_json",
    "inputs_hash",
    "rationale",
    "hypothesis_id",
    "candidate_id",
    "supersedes_id",
)

SETTLEMENT_COLS = (
    "suggestion_id",
    "basis",
    "method_version",
    "status",
    "void_reason",
    "entry_date",
    "exit_date",
    "exit_reason",
    "days_held",
    "entry_credit",
    "gross_pnl",
    "slippage_cost",
    "commission",
    "net_pnl",
    "max_loss",
    "margin",
    "risk_basis",
    "return_on_risk",
    "mfe",
    "mae",
    "fill_basis",
    "detail_json",
)


def _d(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def recent_sessions(conn: Any, symbol: str, end: date, *, days: int = 30) -> list[date]:
    """The symbol's stock sessions in the ``days`` calendar days up to ``end``."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bar_date FROM raw_market.stock_daily
            WHERE symbol = %s AND bar_date BETWEEN %s AND %s AND close > 0
            ORDER BY bar_date
            """,
            (symbol, date.fromordinal(end.toordinal() - days), end),
        )
        return [d for d in (_d(r[0]) for r in cur.fetchall() or []) if d is not None]


def existing_issue_keys(conn: Any, keys: Iterable[str]) -> set[str]:
    wanted = sorted(set(keys))
    if not wanted:
        return set()
    with conn.cursor() as cur:
        cur.execute(f"SELECT issue_key FROM {T_SUGGESTION} WHERE issue_key = ANY(%s)", (wanted,))
        return {str(r[0]) for r in cur.fetchall() or []}


def earlier_version(conn: Any, source: str, source_ref: str, as_of: date, symbol: str, version: str) -> str | None:
    """The latest suggestion an older version of the same rule issued for this session and symbol."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT suggestion_id FROM {T_SUGGESTION}
            WHERE source = %s AND source_ref = %s AND as_of_session = %s AND symbol = %s
              AND source_version <> %s
            ORDER BY issued_at DESC LIMIT 1
            """,
            (source, source_ref, as_of, symbol.upper(), version),
        )
        r = cur.fetchone()
    return str(r[0]) if r else None


def iv_regime(conn: Any, symbol: str, d: date) -> dict[str, Any] | None:
    """The symbol's IV percentile row on ``d`` (the regime label of threshold 2)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT iv_current, iv_percentile_1y, iv_rank_1y, lookback_days
            FROM features.option_metric_iv_percentile_daily
            WHERE symbol = %s AND trade_date = %s
            """,
            (symbol, d),
        )
        r = cur.fetchone()
    if not r:
        return None
    return {"iv_current": r[0], "iv_percentile_1y": r[1], "iv_rank_1y": r[2], "lookback_days": r[3]}


def pine_signals(conn: Any, script_ids: Iterable[str], sessions: Iterable[date]) -> list[dict[str, Any]]:
    """Pine buy / sell rows for ``script_ids`` on ``sessions`` (``features.stock_signal_pine_daily``)."""
    ids = sorted(set(script_ids))
    days = sorted(set(sessions))
    if not ids or not days:
        return []
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT script_id, symbol, trade_date, side, script_version, close
            FROM {TABLE_STOCK_SIGNAL_PINE_DAILY}
            WHERE script_id = ANY(%s) AND trade_date = ANY(%s)
            ORDER BY trade_date, script_id, symbol, side
            """,
            (ids, days),
        )
        rows = cur.fetchall() or []
    return [
        {
            "script_id": str(r[0]),
            "symbol": str(r[1]).upper(),
            "trade_date": _d(r[2]),
            "side": str(r[3]),
            "script_version": int(r[4]),
            "close": None if r[5] is None else float(r[5]),
        }
        for r in rows
    ]


def pine_issued(conn: Any, script_ids: Iterable[str], since: date) -> list[dict[str, Any]]:
    """The Pine suggestions already written for ``script_ids`` since ``since``, one per (script, symbol, session).

    A superseded suggestion and the one that replaced it are the same issue here:
    the caps count sessions a script spoke on a symbol, not rows.
    """
    ids = sorted(set(script_ids))
    if not ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (source_ref, symbol, as_of_session) source_ref, symbol, as_of_session, structure
            FROM {T_SUGGESTION}
            WHERE source = 'pine' AND source_ref = ANY(%s) AND as_of_session >= %s
            ORDER BY source_ref, symbol, as_of_session, issued_at DESC
            """,
            (ids, since),
        )
        rows = cur.fetchall() or []
    return [
        {"script_id": str(r[0]), "symbol": str(r[1]).upper(), "as_of": _d(r[2]), "structure": r[3]}
        for r in rows
    ]


def insert_suggestion(conn: Any, s: Suggestion) -> bool:
    """Append one suggestion; False when its ``issue_key`` is already written."""
    row = s.validate().to_row()
    cols = ", ".join(_SUGGESTION_COLS)
    marks = ", ".join(["%s"] * len(_SUGGESTION_COLS))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {T_SUGGESTION} ({cols}) VALUES ({marks}) ON CONFLICT (issue_key) DO NOTHING RETURNING 1",
            tuple(row[c] for c in _SUGGESTION_COLS),
        )
        return cur.fetchone() is not None


def pending_settlements(
    conn: Any, bases: tuple[str, ...], method_version: str, *, unpaired_source: str = "baseline"
) -> list[dict[str, Any]]:
    """Option suggestions still missing a row for any of ``bases`` at ``method_version``.

    ``unpaired_source`` owes every basis but ``baseline_paired`` (it is the pair).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.suggestion_id, s.as_of_session, s.source, s.symbol, s.structure, s.legs_json,
                   s.take_profit_pct, s.stop_loss_mult, s.exit_dte, s.max_hold_days, s.snapshot_json,
                   ARRAY(SELECT st.basis FROM {T_SETTLEMENT} st
                         WHERE st.suggestion_id = s.suggestion_id AND st.method_version = %s) AS done
            FROM {T_SUGGESTION} s
            WHERE s.kind = 'option_structure'
              AND (SELECT count(*) FROM {T_SETTLEMENT} st
                   WHERE st.suggestion_id = s.suggestion_id
                     AND st.method_version = %s
                     AND st.basis = ANY(%s))
                  < CASE WHEN s.source = %s THEN %s ELSE %s END
            ORDER BY s.as_of_session, s.suggestion_id
            """,
            (
                method_version,
                method_version,
                list(bases),
                unpaired_source,
                len([b for b in bases if b != "baseline_paired"]),
                len(bases),
            ),
        )
        names = [c[0] for c in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall() or []]
    for r in rows:
        for k in ("legs_json", "snapshot_json"):
            if isinstance(r[k], str):
                r[k] = json.loads(r[k])
        r["as_of_session"] = _d(r["as_of_session"])
        r["done"] = set(r["done"] or [])
    return rows


def insert_settlement(conn: Any, row: dict[str, Any]) -> bool:
    payload = {**row, "detail_json": json.dumps(row.get("detail_json") or {}, default=str)}
    cols = ", ".join(SETTLEMENT_COLS)
    marks = ", ".join(["%s"] * len(SETTLEMENT_COLS))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {T_SETTLEMENT} ({cols}) VALUES ({marks})
            ON CONFLICT (suggestion_id, basis, method_version) DO NOTHING RETURNING 1
            """,
            tuple(payload.get(c) for c in SETTLEMENT_COLS),
        )
        return cur.fetchone() is not None


__all__ = [
    "SETTLEMENT_COLS",
    "earlier_version",
    "existing_issue_keys",
    "insert_settlement",
    "insert_suggestion",
    "iv_regime",
    "pending_settlements",
    "pine_issued",
    "pine_signals",
    "recent_sessions",
]
