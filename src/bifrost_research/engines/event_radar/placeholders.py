"""Placeholder Event Radar rows that were never real data.

`run_event_radar` used to upsert a canned sample as ``dagster-fallback`` when
the input directory was empty. File ingest also minted ``ws:*smoke*`` sources
from smoke filenames. Readers skip both; the rows stay until the Owner says
delete them.
"""

from __future__ import annotations

PLACEHOLDER_SOURCES = frozenset({"dagster-fallback"})

# psycopg ``%s`` queries: ``%`` in LIKE must be doubled.
PLACEHOLDER_SQL = (
    "(source = 'dagster-fallback' "
    "OR (source LIKE 'ws:%%' AND source ILIKE '%%smoke%%'))"
)


def is_placeholder_source(source: str | None) -> bool:
    if not source:
        return False
    if source in PLACEHOLDER_SOURCES:
        return True
    return source.startswith("ws:") and "smoke" in source.lower()
