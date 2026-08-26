"""Preset Research agents (Wave RS-E3) — Morning Prep + EOD Review.

Agents only create ``research.ai_draft`` + ``research.ai_action_log`` rows.
They never mutate ``research.hypothesis`` without a user approve click.
"""

from bifrost_research.copilot.agents.eod_review import run_eod_review
from bifrost_research.copilot.agents.morning_prep import run_morning_prep

__all__ = ["run_morning_prep", "run_eod_review"]
