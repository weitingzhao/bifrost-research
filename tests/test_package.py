"""Smoke tests for bifrost_research package."""

from bifrost_research import __version__


def test_version() -> None:
    # Wave RS-E3 · Morning/EOD agents + draft inbox
    assert __version__ == "0.17.1"
