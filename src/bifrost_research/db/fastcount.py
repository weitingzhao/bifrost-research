"""Counting the large feature tables without scanning them (0.116.0).

The ``bifrost`` role runs with ``statement_timeout=2s``. The feature tables
signal-health reports on are 0.5–0.9 GB, and every exact aggregate over them
is a full scan: idle it takes 0.4–3.3s, busy it is cancelled. Three shapes
replace those scans:

- **Rows** — the planner's estimate (``pg_class.reltuples``), summed over the
  leaves of a partitioned table. It is refreshed by autoanalyze after each
  batch write. Exact only for a leaf that holds data and was never analysed.
- **Distinct values** — a walk down an index that leads on the column: one
  probe per distinct value (hundreds), not a read of every row (millions).
- **A breakdown dominated by one value** — count the other values off a
  partial index ``WHERE column <> dominant``, and the dominant one as the
  row total minus the rest. Still correct if another value comes to dominate;
  only slower.

Callers that need exact numbers right after a write (a batch job's own run
summary) keep the exact queries.
"""

from __future__ import annotations

import re
from typing import Any

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")
_LITERAL = re.compile(r"^[a-z_][a-z0-9_]*$")

# A plain table is its own leaf; pg_partition_tree() returns nothing for it.
_ESTIMATE_SQL = """
    SELECT COALESCE(SUM(c.reltuples) FILTER (WHERE c.reltuples >= 0), 0)::bigint,
           COUNT(*) FILTER (WHERE c.reltuples < 0 AND pg_relation_size(c.oid) > 0)
    FROM pg_class c
    WHERE (c.oid = to_regclass(%s) AND c.relkind <> 'p')
       OR c.oid IN (SELECT relid FROM pg_partition_tree(to_regclass(%s)) WHERE isleaf)
"""


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"not a plain identifier: {name!r}")
    return name


def estimate_rows(cur: Any, table: str) -> tuple[int, bool]:
    """``(rows, estimated)`` — the planner's estimate, or an exact count when there is none."""
    cur.execute(_ESTIMATE_SQL, (table, table))
    estimate, unanalysed = cur.fetchone() or (0, 0)
    if unanalysed:
        cur.execute(f"SELECT COUNT(*)::bigint FROM {_ident(table)}")
        return int((cur.fetchone() or (0,))[0] or 0), False
    return int(estimate or 0), True


def distinct_count(cur: Any, table: str, column: str) -> int:
    """``COUNT(DISTINCT column)`` by walking an index that leads on ``column``.

    Each step asks for the next value above the last one, which an index
    answers with a single probe; the column must be NOT NULL (a NULL would
    end the walk early).
    """
    t, c = _ident(table), _ident(column)
    cur.execute(
        f"""
        WITH RECURSIVE walk AS (
            (SELECT {c} AS v FROM {t} ORDER BY {c} LIMIT 1)
            UNION ALL
            SELECT (SELECT {c} FROM {t} WHERE {c} > walk.v ORDER BY {c} LIMIT 1)
            FROM walk
            WHERE walk.v IS NOT NULL
        )
        SELECT COUNT(v)::bigint FROM walk
        """
    )
    return int((cur.fetchone() or (0,))[0] or 0)


def breakdown_with_dominant(
    cur: Any, table: str, column: str, dominant: str, total: int
) -> dict[str, int]:
    """``GROUP BY column`` counts when ``dominant`` holds nearly every row.

    The other values are counted off a partial index whose predicate is
    ``column <> '<dominant>'``; the literal is written into the statement so
    the planner can match that predicate. The dominant count is what is left
    of ``total`` and is never negative.
    """
    t, c = _ident(table), _ident(column)
    if not _LITERAL.match(dominant):
        raise ValueError(f"not a plain value: {dominant!r}")
    cur.execute(f"SELECT {c}, COUNT(*)::bigint FROM {t} WHERE {c} <> '{dominant}' GROUP BY 1")
    counts = {str(r[0]): int(r[1]) for r in cur.fetchall()}
    rest = max(int(total) - sum(counts.values()), 0)
    if rest:
        counts[dominant] = rest
    return counts


__all__ = ["breakdown_with_dominant", "distinct_count", "estimate_rows"]
