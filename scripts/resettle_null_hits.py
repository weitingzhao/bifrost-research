"""Clear the outcome rows that can never be judged, so settlement rebuilds them.

`_forward_leg` took the first bar at or after the trade date; the benchmark leg
asked SPY for its close on the trade date exactly. A candidate proposed on a
Saturday therefore got a real forward return and no benchmark, `hit` was
written NULL, and `_pending` skips any (candidate, horizon) pair that already
has a row — so nothing ever came back for it. The leash's fourth gate wants
five judged outcomes and was being fed these.

The date rule is aligned as of this version. Deleting the unjudgeable rows is
what lets the next settlement recompute them; without it they stay NULL
forever on a table with three rows in it.

Deletes only rows with no benchmark AND no verdict. A row with a real `hit` is
evidence and is never touched.

Dry run by default — this deletes Owner-visible state.

    python -m scripts.resettle_null_hits            # show the plan
    python -m scripts.resettle_null_hits --apply    # delete, then resettle

D10 BLOCKED — reads raw_market.*, writes research.candidate_outcome only.
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
from bifrost_research.engines.candidate_outcome import entry as settlement
from bifrost_research.schema.schemas import TABLE_RESEARCH_CANDIDATE_OUTCOME

_UNJUDGEABLE = "hit IS NULL AND benchmark_return IS NULL"


def plan(conn: Any) -> list[dict[str, Any]]:
    """Every outcome row a resettle would drop, newest first."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, candidate_id, symbol, trade_date, horizon_days,
                   forward_return, settled_at
            FROM {TABLE_RESEARCH_CANDIDATE_OUTCOME}
            WHERE {_UNJUDGEABLE}
            ORDER BY trade_date DESC, symbol, horizon_days
            """
        )
        rows = cur.fetchall() or []
    return [
        {
            "id": r[0],
            "candidate_id": r[1],
            "symbol": r[2],
            "trade_date": r[3],
            "horizon_days": r[4],
            "forward_return": r[5],
            "settled_at": r[6],
        }
        for r in rows
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="delete and resettle (default: dry run)")
    ap.add_argument("--lookback-days", type=int, default=90, help="resettle window")
    args = ap.parse_args()

    conn = connect()
    try:
        rows = plan(conn)
        print(f"outcome rows with a forward return but no benchmark and no verdict: {len(rows)}\n")
        for r in rows:
            fwd = r["forward_return"]
            shown = "—" if fwd is None else format(float(fwd), "+.4f")
            print(
                f"  {r['trade_date']}  {(r['symbol'] or '?'):6} h={r['horizon_days']:<3}"
                f"  fwd={shown}  ({r['candidate_id']})"
            )

        if not rows:
            print("Nothing to do.")
            return 0

        if not args.apply:
            print("\nDry run. Re-run with --apply to delete these and resettle.")
            return 0

        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {TABLE_RESEARCH_CANDIDATE_OUTCOME} WHERE {_UNJUDGEABLE}"
            )
            deleted = cur.rowcount
        conn.commit()
        print(f"\ndeleted: {deleted}")

        result = settlement.run(lookback_days=args.lookback_days)
        print(f"resettled: {result}")

        left = plan(conn)
        print(f"still unjudgeable after resettle: {len(left)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
