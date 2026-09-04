"""Run trace — the ordered record of what a run did, and when.

Events carried `step` and their payload but no time, so a finished run could
only report its total: "5.1s" for the whole loop, with no way to see that three
of those seconds were the universe scan. The console showed six green stages and
nothing about the shape of the work, which is most of what a reader wants from a
run that has already ended.

Stamping happens on append rather than at each of the seventeen call sites: a
site that forgets is a stage that silently has no timing, and the next event
added would start out untimed. Elapsed milliseconds rather than wall-clock,
because the question is where the time went, and monotonic time cannot be pulled
backwards by an NTP correction mid-run.
"""

from __future__ import annotations

import time
from typing import Any


class RunTrace(list[dict[str, Any]]):
    """A list of trace events that records how far into the run each arrived."""

    def __init__(self) -> None:
        super().__init__()
        self._t0 = time.monotonic()

    def append(self, event: dict[str, Any]) -> None:
        # setdefault: a caller that has a better time for an event keeps it.
        event.setdefault("at_ms", int((time.monotonic() - self._t0) * 1000))
        super().append(event)

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)
