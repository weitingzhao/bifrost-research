"""Daily entrypoint — issue the mechanical suggestions and the Pine ones, then settle the ledger.

Runs in the trading-day batch after the husbandry gate, the volatility engine
(the regime label reads its IV percentile for the session) and the Pine build
(``engines/pine``: the session's signals). A Pine issue that fails is reported
and rolled back; it does not stop the baseline, the simulator or settlement.

D10 BLOCKED — writes ``research.suggestion`` / ``research.suggestion_settlement``.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from bifrost_research.db.conn import connect
from bifrost_research.engines.suggestion.issue import run_issue
from bifrost_research.engines.suggestion.pine import run_issue_pine
from bifrost_research.engines.suggestion.settle import run_settle


def run(*, today: date | None = None, settle_only: bool = False) -> dict[str, Any]:
    conn = connect()
    try:
        issued = {"skipped": "settle_only"} if settle_only else run_issue(conn, today=today)
        pine: dict[str, Any] = {"skipped": "settle_only"}
        if not settle_only:
            try:
                pine = run_issue_pine(conn, today=today)
            except Exception as exc:  # noqa: BLE001 — reported in the result; settlement still runs
                conn.rollback()
                pine = {"error": f"{type(exc).__name__}: {exc}"}
        settled = run_settle(conn, today=today)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return {"issued": issued, "pine": pine, "settled": settled}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Suggestion ledger: issue mechanical and Pine sources, settle")
    p.add_argument("--today", type=date.fromisoformat, default=None)
    p.add_argument("--settle-only", action="store_true")
    args = p.parse_args(argv)
    print(json.dumps(run(today=args.today, settle_only=args.settle_only), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
