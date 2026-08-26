#!/usr/bin/env python3
"""Entrypoint: EOD Review Agent (CronJob / local).

Usage:
  BIFROST_EOD_AGENT_DRY_RUN=1 python scripts/run_eod_agent.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    from bifrost_research.copilot.agents.eod_review import run_eod_review

    result = run_eod_review()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
