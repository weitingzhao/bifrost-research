"""SEC 8-K filings → event radar input (financial-text automation).

Reads raw_market.sec_8k_filing (+ the vendor's classification where one
exists) — a read across the domain boundary that D13 allows — and writes any
rows newer than the watermark as one line each into the event-radar workflow
input directory. The bdev watcher then ingests them like any other drop.

Every line is a real filing: symbol, filing date, SEC item numbers, and the
classifier's category/supporting text when the vendor produced one. Nothing
is invented; text fields are passed through, truncated for line hygiene.

Watermark is fetched_at (not filing_date) so a backfill that inserts old
filings still flows. State lives beside the input directory.

D10 BLOCKED / advisory only.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bifrost_research.db.conn import connect

WORKFLOW_DIR = Path(
    os.environ.get(
        "EVENT_RADAR_WORKFLOW_DIR",
        str(Path.home() / "Desktop/stocks/Research-workspace/事件雷达工作流"),
    )
)
INPUT_DIR = WORKFLOW_DIR / "input"
STATE_PATH = WORKFLOW_DIR / ".sec-8k-watermark.json"
MAX_ROWS = 400

QUERY = """
SELECT f.symbol, f.filing_date, f.items, f.items_text, f.fetched_at,
       d.primary_category, d.supporting_text
FROM raw_market.sec_8k_filing f
LEFT JOIN LATERAL (
    SELECT primary_category, supporting_text
    FROM raw_market.sec_8k_disclosure d
    WHERE d.accession_number = f.accession_number AND d.symbol = f.symbol
    ORDER BY d.fetched_at DESC
    LIMIT 1
) d ON true
WHERE f.fetched_at > %s
ORDER BY f.fetched_at
LIMIT %s
"""


def load_watermark() -> str:
    try:
        return json.loads(STATE_PATH.read_text())["fetched_at"]
    except Exception:
        return "1970-01-01T00:00:00+00:00"


def save_watermark(ts: str) -> None:
    STATE_PATH.write_text(json.dumps({"fetched_at": ts}))


def one_line(row: tuple) -> str:
    """One radar line per filing. items_text is the FULL filing body (up to
    ~240KB with hundreds of newlines) — never embed it raw: the ingest splits
    on newlines, so one unflattened filing became thousands of junk events on
    2026-09-24. Item numbers come from the items array; body text only as a
    whitespace-collapsed snippet, and the whole line is flattened again at the
    end as a hard guarantee."""
    symbol, filing_date, items, items_text, _fetched, category, support = row
    bits = [f"{filing_date} {symbol} filed an 8-K"]
    if items:
        bits.append(f"(items {', '.join(items)})")
    if "2.02" in (items or []):
        bits.append("— a results announcement")
    if category:
        bits.append(f"— classified {category}")
    snippet = support or items_text
    if snippet:
        clean = " ".join(str(snippet).split())[:200]
        bits.append(f": {clean}")
    line = " ".join(bits) + ". reported via the SEC filings feed."
    return " ".join(line.split())


def main() -> int:
    conn = connect()
    try:
        cur = conn.cursor()
        wm = load_watermark()
        cur.execute(QUERY, (wm, MAX_ROWS))
        rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        print(f"sec source: nothing newer than {wm}")
        return 0
    lines = [one_line(r) for r in rows]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = INPUT_DIR / f"sec-8k-{stamp}.md"
    out.write_text("\n".join(lines) + "\n")
    newest = max(r[4] for r in rows)
    save_watermark(newest.isoformat() if hasattr(newest, "isoformat") else str(newest))
    print(f"sec source: wrote {len(lines)} filings -> {out.name} (watermark {newest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
