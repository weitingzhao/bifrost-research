"""Smoke tests for bifrost_research package."""

from bifrost_research import __version__


def test_version() -> None:
    # Wave RS-F5 · Portfolio Context tools + agent; 0.29.0 client_context
    assert __version__ == "0.30.0"
