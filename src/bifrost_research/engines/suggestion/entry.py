"""Daily entrypoint — issue the mechanical suggestions, then settle the ledger.

Runs in the trading-day batch after the husbandry gate and the volatility
engine (the regime label reads its IV percentile for the session).

D10 BLOCKED — writes ``research.suggestion`` / ``research.suggestion_settlement``.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from bifrost_research.db.conn import connect
from bifrost_research.engines.suggestion.issue import run_issue
from bifrost_research.engines.suggestion.settle import run_settle


def run(*, today: date | None = None, settle_only: bool = False) -> dict[str, Any]:
    conn = connect()
    try:
        issued = {"skipped": "settle_only"} if settle_only else run_issue(conn, today=today)
        settled = run_settle(conn, today=today)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return {"issued": issued, "settled": settled}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Suggestion ledger: issue mechanical sources, settle")
    p.add_argument("--today", type=date.fromisoformat, default=None)
    p.add_argument("--settle-only", action="store_true")
    args = p.parse_args(argv)
    print(json.dumps(run(today=args.today, settle_only=args.settle_only), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
