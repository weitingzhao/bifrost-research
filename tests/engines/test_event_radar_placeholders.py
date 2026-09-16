"""Placeholder Event Radar sources are never treated as real data."""

from __future__ import annotations

import inspect

from bifrost_research.api.wave4 import event_calendar, list_event_radar
from bifrost_research.engines.event_radar.placeholders import (
    PLACEHOLDER_SOURCES,
    PLACEHOLDER_SQL,
    is_placeholder_source,
)


def test_placeholder_sources_constant() -> None:
    # ws:sample joined the rule in C1: the shipped sample file's rows (WSSAMP-…,
    # "MegaCorp IPO") are as canned as the dagster fallback's.
    assert PLACEHOLDER_SOURCES == {"dagster-fallback", "ws:sample"}


def test_is_placeholder_source() -> None:
    assert is_placeholder_source("dagster-fallback") is True
    assert is_placeholder_source("ws:sample") is True
    assert is_placeholder_source("ws:k8s-smoke") is True
    assert is_placeholder_source("ws:unit-smoke-file") is True
    assert is_placeholder_source("ws:desk-notes") is False
    assert is_placeholder_source("file-ingest") is False
    assert is_placeholder_source(None) is False
    assert is_placeholder_source("") is False


def test_the_sql_and_the_function_judge_the_same_sources() -> None:
    # The SQL is built from the constants the function reads, so a source can never
    # be hidden by one and kept by the other.
    for source in PLACEHOLDER_SOURCES:
        assert f"'{source}'" in PLACEHOLDER_SQL
        assert is_placeholder_source(source) is True
    assert "'ws:%%'" in PLACEHOLDER_SQL and "'%%smoke%%'" in PLACEHOLDER_SQL


def test_read_endpoints_exclude_placeholders() -> None:
    assert "excluded_placeholder_rows" in inspect.getsource(list_event_radar)
    assert "PLACEHOLDER_SQL" in inspect.getsource(list_event_radar)
    assert "excluded_placeholder_rows" in inspect.getsource(event_calendar)
    assert "PLACEHOLDER_SQL" in inspect.getsource(event_calendar)
