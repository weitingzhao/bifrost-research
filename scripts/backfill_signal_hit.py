#!/usr/bin/env python3
"""Backfill features.stock_signal_lens_hit_daily for one or more lenses.

Usage:
  python scripts/backfill_signal_hit.py --lens skew --days 252
  python scripts/backfill_signal_hit.py --lens skew,gex_regime,terrain_regime --days 252 --clear

Thin wrapper over ``bifrost_research.engines.signal_hit.entry.run`` — the Dagster
asset builds the last few days; this walks back further after a lens is added.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bifrost_research.engines.signal_hit import entry  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lens", required=True, help="comma-separated decay lens ids")
    parser.add_argument("--days", type=int, default=252, help="trading days to walk back")
    parser.add_argument("--clear", action="store_true", help="delete the lenses' rows first")
    args = parser.parse_args(argv)
    lens_args = ["--lens", args.lens, "--lookback-days", str(args.days)]
    if args.clear:
        lens_args.append("--clear")
    return entry.main(lens_args)


if __name__ == "__main__":
    raise SystemExit(main())
