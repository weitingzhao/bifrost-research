"""Placeholder Event Radar rows that were never real data.

`run_event_radar` used to upsert a canned sample as ``dagster-fallback`` when the
input directory was empty; file ingest minted ``ws:*smoke*`` sources from smoke
filenames, and ``ws:sample`` from the shipped sample file (its ids read
``WSSAMP-…`` and its text announces a MegaCorp IPO). Readers skip all of them.

The rule lives here once. ``PLACEHOLDER_SQL`` is built from the same constants
``is_placeholder_source`` reads, so the two cannot answer differently — a row the
readers hide is exactly a row the purge deletes.
"""

from __future__ import annotations

from typing import Iterable

#: Sources that are placeholders by name.
PLACEHOLDER_SOURCES = frozenset({"dagster-fallback", "ws:sample"})
#: A smoke-test file name became its own source: ``ws:<filename>`` carrying "smoke".
SMOKE_PREFIX = "ws:"
SMOKE_MARK = "smoke"


def _sql_list(values: Iterable[str]) -> str:
    return ", ".join(f"'{v}'" for v in sorted(values))


# psycopg ``%s`` queries: ``%`` in LIKE must be doubled. Every literal below comes
# from the constants above, never from a request.
PLACEHOLDER_SQL = (
    f"(source IN ({_sql_list(PLACEHOLDER_SOURCES)}) "
    f"OR (source LIKE '{SMOKE_PREFIX}%%' AND source ILIKE '%%{SMOKE_MARK}%%'))"
)


def is_placeholder_source(source: str | None) -> bool:
    if not source:
        return False
    if source in PLACEHOLDER_SOURCES:
        return True
    return source.startswith(SMOKE_PREFIX) and SMOKE_MARK in source.lower()
