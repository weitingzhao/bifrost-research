"""Re-theme existing radar rows with the pipeline's theme matcher.

The matcher only runs at ingest time, so rows written before a theme line was
registered (or while the SEC backfill drains) stay blank. This walks every
non-dropped row with theme = '' and stamps the ones the matcher now claims.
Idempotent and re-runnable; rows the matcher does not claim stay blank —
blank is honest (registry rule). D13: research writing its own features.*.
"""
from __future__ import annotations

import sys

from bifrost_research.db.conn import connect
from bifrost_research.engines.event_radar.pipeline import _match_theme


def main() -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_id, raw_text
                FROM features.event_signal_radar_daily
                WHERE dropped IS DISTINCT FROM true
                  AND (theme IS NULL OR theme = '')
                """
            )
            rows = cur.fetchall()
            updates = [
                (theme, event_id)
                for event_id, raw_text in rows
                if (theme := _match_theme(raw_text or ""))
            ]
            for theme, event_id in updates:
                cur.execute(
                    "UPDATE features.event_signal_radar_daily SET theme = %s WHERE event_id = %s",
                    (theme, event_id),
                )
        conn.commit()
    finally:
        conn.close()
    print(f"retheme: scanned {len(rows)} blank rows, stamped {len(updates)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
