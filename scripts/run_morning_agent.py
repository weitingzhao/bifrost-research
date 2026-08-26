#!/usr/bin/env python3
"""Entrypoint: Morning Prep Agent (CronJob / local).

Usage:
  BIFROST_MORNING_AGENT_DRY_RUN=1 python scripts/run_morning_agent.py
  python -m bifrost_research.copilot.agents.morning_prep  # via run()
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/run_morning_agent.py` without install
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    from bifrost_research.copilot.agents.morning_prep import run_morning_prep

    result = run_morning_prep()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
