"""Write down the refusals the Owner already made.

Dismissing a Decision Inbox draft used to write `research.ai_draft` and
nothing else, so `research.candidate_pool` — the table the loop reads before
proposing — never learned. Every name the Owner declined stayed `open` and
came back the next morning.

The code path is fixed going forward. This closes the gap behind it: without
a backfill the decline gate ships with an empty memory and suppresses nobody
on day one, and the symptom the fix exists to remove persists for as long as
it takes new refusals to accumulate.

Reads dismissed `candidate_batch` drafts and marks their still-open pool rows
dismissed. Guarded by `status = 'open'`, so a name since promoted, expired or
already dismissed is left exactly as it is.

Dry run by default — this changes Owner-visible state.

    python -m scripts.backfill_declined_candidates            # show the plan
    python -m scripts.backfill_declined_candidates --apply    # write it

D10 BLOCKED — writes research.candidate_pool only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bifrost_research.db.conn import connect
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import candidate_pool as cand_repo


def plan(conn: Any, *, limit: int) -> list[dict[str, Any]]:
    """Every (draft, candidate) pair a backfill would touch, newest first."""
    drafts = draft_repo.list_drafts(conn, status="dismissed", kind="candidate_batch", limit=limit)
    out: list[dict[str, Any]] = []
    for draft in drafts:
        payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else {}
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or item.get("candidate_id") or "").strip()
            if not cid:
                continue
            row = cand_repo.get_candidate(conn, cid)
            if row is None:
                continue
            out.append(
                {
                    "draft_id": draft.get("id"),
                    "dismissed_at": str(draft.get("created_at") or "")[:10],
                    "candidate_id": cid,
                    "symbol": row.get("symbol"),
                    "trade_date": str(row.get("trade_date") or "")[:10],
                    "status": row.get("status"),
                    "objective_id": (row.get("source_ref") or {}).get("objective_id"),
                }
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--limit", type=int, default=200, help="dismissed drafts to scan")
    args = ap.parse_args()

    conn = connect()
    try:
        rows = plan(conn, limit=args.limit)
        actionable = [r for r in rows if r["status"] == "open"]

        print(f"dismissed candidate_batch drafts scanned: limit={args.limit}")
        print(f"candidate rows behind them: {len(rows)}")
        print(f"still open, would be marked dismissed: {len(actionable)}\n")
        for r in actionable:
            print(
                f"  {r['trade_date']}  {(r['symbol'] or '?'):6}  {r['candidate_id']}"
                f"  (draft {r['draft_id']} dismissed {r['dismissed_at']})"
            )
        skipped = [r for r in rows if r["status"] != "open"]
        if skipped:
            print(f"\n  left alone ({len(skipped)}): already " + ", ".join(
                sorted({str(r["status"]) for r in skipped})
            ))

        if not args.apply:
            print("\nDry run. Re-run with --apply to write.")
            return 0

        written = 0
        for r in actionable:
            if cand_repo.dismiss_candidate(conn, r["candidate_id"]) is not None:
                written += 1
        print(f"\nmarked dismissed: {written}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
