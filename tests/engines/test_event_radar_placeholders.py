"""Placeholder Event Radar sources are never treated as real data."""

from __future__ import annotations

import inspect

from bifrost_research.api.wave4 import event_calendar, list_event_radar
from bifrost_research.engines.event_radar.placeholders import (
    PLACEHOLDER_SOURCES,
    is_placeholder_source,
)


def test_placeholder_sources_constant() -> None:
    assert PLACEHOLDER_SOURCES == {"dagster-fallback"}


def test_is_placeholder_source() -> None:
    assert is_placeholder_source("dagster-fallback") is True
    assert is_placeholder_source("ws:k8s-smoke") is True
    assert is_placeholder_source("ws:unit-smoke-file") is True
    assert is_placeholder_source("ws:desk-notes") is False
    assert is_placeholder_source("file-ingest") is False
    assert is_placeholder_source(None) is False
    assert is_placeholder_source("") is False


def test_read_endpoints_exclude_placeholders() -> None:
    assert "excluded_placeholder_rows" in inspect.getsource(list_event_radar)
    assert "PLACEHOLDER_SQL" in inspect.getsource(list_event_radar)
    assert "excluded_placeholder_rows" in inspect.getsource(event_calendar)
    assert "PLACEHOLDER_SQL" in inspect.getsource(event_calendar)
