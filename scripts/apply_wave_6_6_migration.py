#!/usr/bin/env python3
"""Apply Wave 6.6 legacy features_* schema drop + idempotent DDL refresh."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = ROOT / "scripts" / "wave_6_6_drop_legacy_features_schemas.sql"


def main() -> int:
    if not SQL_PATH.is_file():
        print(f"Missing migration SQL: {SQL_PATH}", file=sys.stderr)
        return 1
    sql = SQL_PATH.read_text(encoding="utf-8")
    from bifrost_research.db.conn import connect
    from bifrost_research.schema.ddl import apply_all_ddl

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print("wave_6_6_drop_legacy_features_schemas.sql applied")
        apply_all_ddl(conn)
        print("apply_all_ddl complete (includes legacy schema drop)")
    except Exception as exc:
        conn.rollback()
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
