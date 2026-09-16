"""Delete the Event Radar rows that were never data (Owner authorised 2026-09-15).

Three sources ever wrote them: ``dagster-fallback`` (the canned sample the empty
inbox used to upsert), ``ws:*smoke*`` (smoke files) and ``ws:sample`` (the shipped
sample file). The readers already hide them; this removes them from the table.

Two guards, because deleting rows is not a thing to do by accident:

- ``force=False`` (the default) counts and reports, and runs no DELETE at all;
- the WHERE clause is ``placeholders.PLACEHOLDER_SQL`` — the same predicate the
  read endpoints exclude by, not a second copy that could drift wider.

The readers keep their exclusion afterwards: this run is once, the rule has to go
on catching anything canned that arrives later. Run by hand from Dagster; nothing
schedules it. D10 BLOCKED — advisory data only.
"""

from __future__ import annotations

import logging
from typing import Any

from bifrost_research.db.conn import connect
from bifrost_research.engines.event_radar.placeholders import PLACEHOLDER_SQL
from bifrost_research.schema.schemas import TABLE_EVENT_SIGNAL_RADAR_DAILY

logger = logging.getLogger(__name__)


def count_by_source(conn: Any) -> dict[str, int]:
    """How many placeholder rows each source is holding, right now."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT source, COUNT(*)::bigint
            FROM {TABLE_EVENT_SIGNAL_RADAR_DAILY}
            WHERE {PLACEHOLDER_SQL}
            GROUP BY source
            ORDER BY source
            """
        )
        return {str(row[0]): int(row[1]) for row in cur.fetchall() or []}


def delete_placeholders(conn: Any) -> int:
    """Delete every row the predicate matches; returns the row count."""
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {TABLE_EVENT_SIGNAL_RADAR_DAILY} WHERE {PLACEHOLDER_SQL}")
        deleted = cur.rowcount if cur.rowcount is not None else 0
    conn.commit()
    return int(deleted)


def run(*, force: bool = False) -> dict[str, Any]:
    conn = connect()
    try:
        before = count_by_source(conn)
        total = sum(before.values())
        if not force:
            return {
                "engine": "event_radar_purge",
                "mode": "dry_run",
                "predicate": PLACEHOLDER_SQL,
                "matched_by_source": before,
                "matched": total,
                "deleted": 0,
                "note": "dry run — nothing deleted; call with force=True to delete",
            }
        deleted = delete_placeholders(conn)
        after = count_by_source(conn)
        logger.info("event_radar_purge deleted %s placeholder rows", deleted)
        return {
            "engine": "event_radar_purge",
            "mode": "deleted",
            "predicate": PLACEHOLDER_SQL,
            "matched_by_source": before,
            "matched": total,
            "deleted": deleted,
            "remaining_by_source": after,
        }
    finally:
        conn.close()
