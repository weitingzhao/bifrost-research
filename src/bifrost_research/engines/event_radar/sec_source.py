"""SEC 8-K filings -> Event Radar, in the cluster (TD-100).

Reads ``raw_market.sec_8k_filing`` (+ the vendor's classification where one
exists) -- a read across the domain boundary that D13 allows -- turns each
filing into one radar line, and runs the lines through the Event Radar pipeline
into ``features.event_signal_radar_daily``.

Until 2026-10 this ran on the Owner's Mac (``scripts/event_radar_watch.sh``
under bdev): it wrote lines into the workspace input directory with a file
watermark, and the watcher ingested the file. That loop read ``.env`` once and
swallowed every failure; after a password rotation it failed 157 ticks over
~35 hours while the cluster's event-radar schedule stayed green with nothing to
do. Here a failure raises, so the Dagster run fails and the failure sensor
alerts.

No watermark. A filing is new when its head -- ``"<filing_date> <symbol> filed
an 8-K (items ...)"``, the start of every line -- is not already in the table
as many times as the filings that share it. That makes a run idempotent: it
can re-read a week of filings, it does not duplicate what the Mac loop already
ingested, and a missed tick is caught up by the next one.

D10 BLOCKED / advisory only.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# The plugin's fundamentals-market slot writes the filings once a day, around
# 04:30 UTC Tue-Sat; a week of them is ~1,000 rows.
LOOKBACK_DAYS = 7
MAX_LINES = 400
SOURCE_PREFIX = "ws:sec-8k-"
# Rows ingested on the Mac took collected_at from its local date (Central). The
# 04:30 UTC batch is the previous session's filings, so the same zone keeps the
# 17k existing rows and the new ones on one calendar.
COLLECTED_TZ = ZoneInfo("America/Chicago")

# items_text is the full filing body (up to ~240 KB); a line uses at most a
# 200-character snippet of it, so never pull more than this much.
_SNIPPET_SOURCE_CHARS = 2000

QUERY = """
SELECT f.symbol, f.filing_date, f.items, left(f.items_text, %(snippet)s), f.fetched_at,
       d.primary_category, d.supporting_text
FROM raw_market.sec_8k_filing f
LEFT JOIN LATERAL (
    SELECT primary_category, supporting_text
    FROM raw_market.sec_8k_disclosure d
    WHERE d.accession_number = f.accession_number AND d.symbol = f.symbol
    ORDER BY d.fetched_at DESC
    LIMIT 1
) d ON true
WHERE f.fetched_at > %(since)s
ORDER BY f.fetched_at, f.accession_number, f.symbol
"""

EXISTING_QUERY = """
SELECT raw_text
FROM features.event_signal_radar_daily
WHERE source LIKE %(prefix)s
  AND collected_at >= %(since)s
"""

_HEAD_RE = re.compile(r"^(\S+ \S+ filed an 8-K(?: \(items [^)]*\))?)")


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def filing_head(symbol: str, filing_date: Any, items: Sequence[str] | None) -> str:
    """The part of a radar line that names the filing (not its later classification)."""
    head = f"{filing_date} {symbol} filed an 8-K"
    if items:
        head += f" (items {', '.join(items)})"
    return _flat(head)


def head_of_line(raw_text: str) -> str | None:
    match = _HEAD_RE.match(raw_text or "")
    return match.group(1) if match else None


def one_line(row: Sequence[Any]) -> str:
    """One radar line per filing.

    items_text is the FULL filing body with hundreds of newlines -- never embed
    it raw: the ingest splits on newlines, so one unflattened filing became
    thousands of junk events on 2026-09-24. Item numbers come from the items
    array; body text only as a whitespace-collapsed snippet, and the whole line
    is flattened again at the end as a hard guarantee.
    """
    symbol, filing_date, items, items_text, _fetched, category, support = row
    bits = [filing_head(symbol, filing_date, items)]
    if "2.02" in (items or []):
        bits.append("— a results announcement")
    if category:
        bits.append(f"— classified {category}")
    snippet = support or items_text
    if snippet:
        bits.append(f": {_flat(snippet)[:200]}")
    return _flat(" ".join(bits) + ". reported via the SEC filings feed.")


def select_new_lines(
    rows: Sequence[Sequence[Any]],
    existing_raw_texts: Sequence[str],
    *,
    limit: int = MAX_LINES,
) -> list[str]:
    """Lines for filings not yet in the table, oldest fetched first.

    Counts, not a set: two filings can share a head (same symbol, date and
    items -- an amendment), and the second must still get its line once.
    """
    seen = Counter(h for h in (head_of_line(t) for t in existing_raw_texts) if h)
    out: list[str] = []
    for row in rows:
        head = filing_head(row[0], row[1], row[2])
        if seen[head] > 0:
            seen[head] -= 1
            continue
        out.append(one_line(row))
        if len(out) >= limit:
            break
    return out


@dataclass
class SecIngestResult:
    filings_read: int
    lines_new: int
    rows_written: int
    kept: int
    dropped: int
    newest_fetched_at: str | None
    source: str | None
    batch_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": "event_radar",
            "mode": "sec_8k",
            "filings_read": self.filings_read,
            "lines_new": self.lines_new,
            "rows_written": self.rows_written,
            "kept": self.kept,
            "dropped": self.dropped,
            "newest_fetched_at": self.newest_fetched_at,
            "source": self.source,
            "batch_id": self.batch_id,
            "advisory": "D10 BLOCKED — event radar is advisory only (D13 OLAP)",
        }


def ingest_sec_filings(
    conn: Any,
    *,
    now: datetime | None = None,
    lookback_days: int = LOOKBACK_DAYS,
    limit: int = MAX_LINES,
) -> SecIngestResult:
    """Read new filings and upsert their radar lines. Raises on any DB error."""
    from bifrost_research.engines.event_radar.pipeline import run_pipeline, upsert_events

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=lookback_days)
    # Two days of slack: a filing fetched at the window's edge was ingested on
    # the Mac's calendar day, which can be the day before in UTC.
    existing_since: date = (since - timedelta(days=2)).date()
    with conn.cursor() as cur:
        cur.execute(QUERY, {"since": since, "snippet": _SNIPPET_SOURCE_CHARS})
        rows = cur.fetchall()
        cur.execute(EXISTING_QUERY, {"prefix": f"{SOURCE_PREFIX}%", "since": existing_since})
        existing = [r[0] for r in cur.fetchall()]
    # Close the read transaction before the write.
    conn.rollback()

    newest = max((r[4] for r in rows), default=None)
    newest_iso = newest.isoformat() if hasattr(newest, "isoformat") else None
    lines = select_new_lines(rows, existing, limit=limit)
    if not lines:
        return SecIngestResult(len(rows), 0, 0, 0, 0, newest_iso, None, None)

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    source = f"{SOURCE_PREFIX}{stamp}"
    result = run_pipeline(
        "\n".join(lines),
        source=source,
        collected_at=now.astimezone(COLLECTED_TZ).date(),
        batch_id=f"sec-{uuid4().hex[:10]}",
    )
    written = upsert_events(conn, result)
    logger.info(
        "sec 8-K: %d filings read, %d new lines, %d rows written (%s)",
        len(rows),
        len(lines),
        written,
        source,
    )
    return SecIngestResult(
        filings_read=len(rows),
        lines_new=len(lines),
        rows_written=written,
        kept=len(result.kept),
        dropped=len(result.dropped),
        newest_fetched_at=newest_iso,
        source=source,
        batch_id=result.batch_id,
    )


def main() -> int:
    """Manual run: ``python -m bifrost_research.engines.event_radar.sec_source``."""
    from bifrost_research.db.conn import connect

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = connect()
    try:
        logger.info("result=%s", ingest_sec_filings(conn).to_dict())
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
