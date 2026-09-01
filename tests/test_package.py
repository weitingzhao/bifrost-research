"""Smoke tests for bifrost_research package."""

from bifrost_research import __version__


def test_version() -> None:
    # Analyze Waves A/B/C (0.36) after IDS IV Solver (0.35) + Waves 12-15.
    assert __version__ == "0.57.0"
