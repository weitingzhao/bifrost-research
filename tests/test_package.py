"""Smoke tests for bifrost_research package."""

from bifrost_research import __version__


def test_version() -> None:
    assert __version__ == "0.8.0"
