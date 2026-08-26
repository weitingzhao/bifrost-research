"""Smoke tests for bifrost_research package."""

from bifrost_research import __version__


def test_version() -> None:
    # Wave RS-F5 · Portfolio Context tools + agent
    assert __version__ == "0.18.0"
