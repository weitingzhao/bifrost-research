"""Idempotent schema apply entrypoint — Analyze Wave E DDL Job.

Usage::

    python -m bifrost_research.schema.init --apply
    python -m bifrost_research.schema.init --apply --scope features
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply Research features DDL (idempotent)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Connect and run DDL against ANALYTICS_PG_*",
    )
    parser.add_argument(
        "--scope",
        choices=("features", "all"),
        default="features",
        help="features = Feature Store only (default); all = features + research workflow",
    )
    parser.add_argument(
        "--tables",
        type=str,
        default="",
        help="Optional comma list (informational; DDL ensure is full idempotent)",
    )
    args = parser.parse_args(argv)
    if not args.apply:
        print("Pass --apply to run DDL against ANALYTICS_PG_*")
        return 2

    from bifrost_research.db.conn import connect
    from bifrost_research.schema.ddl import apply_all_ddl, apply_features_ddl

    conn = connect()
    try:
        if args.scope == "all":
            apply_all_ddl(conn)
            print("apply_all_ddl complete")
        else:
            apply_features_ddl(conn)
            print("apply_features_ddl complete")
        if args.tables.strip():
            print(f"requested focus: {args.tables}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
