"""Wave D scanner CronJob scheduler shim."""

from __future__ import annotations

import sys

from bifrost_research.engines.scan.entry import main, run

__all__ = ["main", "run"]


if __name__ == "__main__":
    sys.exit(main())
