#!/usr/bin/env python3
"""Seed Loop Orchestrator objectives — Wave LO-0 + LS-1 Stock-first.

Usage:
  python scripts/seed_loop_objective.py --dry-run
  python scripts/seed_loop_objective.py --apply
  python scripts/seed_loop_objective.py --apply --profile stock
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bifrost_research.copilot.harness.policy_schema import default_stock_composite_policy

DEFAULT_SCAN_POLICY: dict[str, object] = {
    "universe_mode": "scan_legacy",
    "preset": "adaptive_30d",
    "flag_filter": [],
    "min_composite_score": 0.55,
    "min_hit_rate": 0.45,
    "max_candidates": 8,
    "use_llm_plan": True,
    "auto_validate": True,
}

PROFILES: dict[str, dict[str, object]] = {
    "scan": {
        "objective_id": "obj-daily-loop-scan",
        "title": "Daily Loop Scan",
        "description": (
            "Legacy option-scan path over features.stock_signal_scan_daily; "
            "propose batch for Decision Inbox."
        ),
        "policy_json": DEFAULT_SCAN_POLICY,
    },
    "stock": {
        "objective_id": "obj-daily-loop-stock",
        "title": "Daily Loop Stock Explorer",
        "description": (
            "Stock-first composite funnel (SEPA / Momentum / Events) with optional "
            "option overlay; aligned with Discover Stock Explorer."
        ),
        "policy_json": default_stock_composite_policy(),
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed Loop Orchestrator objective")
    parser.add_argument("--dry-run", action="store_true", help="Print payload only")
    parser.add_argument("--apply", action="store_true", help="Insert into research.objective")
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES.keys()),
        default="scan",
        help="scan=legacy option scan; stock=LS-1 stock_composite",
    )
    parser.add_argument("--objective-id", default=None, help="Override profile default id")
    args = parser.parse_args(argv)

    profile = PROFILES[args.profile]
    objective_id = args.objective_id or str(profile["objective_id"])
    payload = {
        "objective_id": objective_id,
        "title": profile["title"],
        "description": profile["description"],
        "schedule": "daily_open",
        "persona": "loop_curator",
        "policy_json": profile["policy_json"],
    }

    if args.dry_run or not args.apply:
        print(json.dumps(payload, indent=2))
        if not args.apply:
            print("\n(dry-run — pass --apply to insert)", file=sys.stderr)
            return 0

    from bifrost_research.db.conn import connect
    from bifrost_research.repositories import objective as obj_repo

    conn = connect()
    try:
        existing = obj_repo.get_objective(conn, objective_id)
        if existing is not None:
            print(f"objective already exists: {objective_id}", file=sys.stderr)
            print(json.dumps(existing, indent=2, default=str))
            return 0
        row = obj_repo.create_objective(
            conn,
            title=str(payload["title"]),
            description=str(payload["description"]),
            schedule=str(payload["schedule"]),
            policy_json=dict(payload["policy_json"]),
            persona=str(payload["persona"]),
            objective_id=objective_id,
        )
        print(json.dumps(row, indent=2, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
