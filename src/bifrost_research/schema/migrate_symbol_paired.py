"""One-off: let research.suggestion_settlement take basis = 'symbol_paired' (S4 control).

The ledger DDL creates the table with every basis in its CHECK, but a table
created before symbol_paired keeps the old CHECK (CREATE TABLE IF NOT EXISTS
does not touch it). A CHECK cannot be altered in place, so this swaps it in one
transaction. Kept out of ``suggestion_ledger_ddl`` so the recurring init path
stays DROP-free; run by hand, after the Owner has approved it.

Forward-only once a symbol_paired row exists: narrowing the CHECK again would
need those rows gone, and the append-only trigger refuses the DELETE.

    python -m bifrost_research.schema.migrate_symbol_paired            # print the plan
    python -m bifrost_research.schema.migrate_symbol_paired --apply    # run it
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from bifrost_research.schema.schemas import SCHEMA_RESEARCH
from bifrost_research.schema.suggestion_ledger_ddl import SETTLEMENT_BASES

CONSTRAINT = "suggestion_settlement_basis_check"
TABLE = f"{SCHEMA_RESEARCH}.suggestion_settlement"


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def statements() -> list[str]:
    return [
        "SET LOCAL lock_timeout = '5s'",
        f"ALTER TABLE {TABLE} DROP CONSTRAINT {CONSTRAINT}",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} CHECK (basis IN ({_in(SETTLEMENT_BASES)}))",
    ]


def current_definition(cur: Any) -> str | None:
    cur.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s::regclass AND conname = %s",
        (TABLE, CONSTRAINT),
    )
    r = cur.fetchone()
    return str(r[0]) if r else None


def apply(conn: Any) -> dict[str, Any]:
    with conn.cursor() as cur:
        before = current_definition(cur)
        if before is not None and "symbol_paired" in before:
            conn.rollback()
            return {"applied": False, "reason": "already widened", "definition": before}
        for sql in statements():
            cur.execute(sql)
        after = current_definition(cur)
    conn.commit()
    return {"applied": True, "before": before, "after": after}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true")
    a = p.parse_args(argv)
    if not a.apply:
        print("\n".join(s + ";" for s in statements()))
        return 0
    from bifrost_research.db.conn import connect  # noqa: PLC0415

    conn = connect()
    try:
        print(apply(conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
