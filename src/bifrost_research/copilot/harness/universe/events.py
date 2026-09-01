"""Event radar universe adapter — reads features.event_signal_radar_daily."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Protocol

from bifrost_research.copilot.harness.policy_schema import EventsLayerPolicy, LoopPolicy

logger = logging.getLogger(__name__)


class _Connection(Protocol):
    def cursor(self) -> Any: ...


def _parse_affected_symbols(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(s).strip().upper() for s in raw if str(s).strip()]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(s).strip().upper() for s in parsed if str(s).strip()]
            except json.JSONDecodeError:
                pass
        return [s.strip().upper() for s in text.split(",") if s.strip()]
    return []


def fetch_event_symbols(
    conn: _Connection,
    *,
    layer: EventsLayerPolicy | None = None,
    limit: int = 500,
) -> tuple[list[str], dict[str, dict[str, Any]], str]:
    layer = layer or EventsLayerPolicy()
    cutoff = date.today() - timedelta(days=layer.within_days)

    sql = """
        SELECT event_id, affected_symbols, importance, theme, event_summary,
               event_date, direction, collected_at
        FROM features.event_signal_radar_daily
        WHERE (dropped IS NULL OR dropped = false)
          AND importance >= %s
          AND (
            event_date >= %s
            OR collected_at >= %s
          )
        ORDER BY importance DESC NULLS LAST, collected_at DESC NULLS LAST
        LIMIT %s
    """
    params = (layer.min_importance, cutoff, cutoff, max(1, min(limit * 3, 2000)))

    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall() or []]
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_event_symbols failed: %s", exc)
        return [], {}, f"query failed: {exc}"

    meta: dict[str, dict[str, Any]] = {}
    symbols: list[str] = []
    for row in rows:
        importance = row.get("importance")
        for sym in _parse_affected_symbols(row.get("affected_symbols")):
            if sym not in meta:
                symbols.append(sym)
                meta[sym] = {
                    "event_importance": importance,
                    "event_theme": row.get("theme"),
                    "event_summary": row.get("event_summary"),
                    "event_date": (
                        row["event_date"].isoformat()
                        if hasattr(row.get("event_date"), "isoformat")
                        else row.get("event_date")
                    ),
                }
            elif importance and (meta[sym].get("event_importance") or 0) < importance:
                meta[sym]["event_importance"] = importance

    filt = f"importance>={layer.min_importance}, within_days={layer.within_days}"
    return symbols[:limit], meta, filt


def resolve_events_only(
    conn: _Connection,
    policy: LoopPolicy,
    *,
    limit: int,
) -> tuple[list[str], dict[str, dict[str, Any]], str]:
    return fetch_event_symbols(conn, layer=policy.layers.events, limit=limit)
