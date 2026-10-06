"""Event definitions for the event-driven backtest query engine (Wave RS-C1).

An ``EventDef`` names *when* to run a strategy template. Each ``kind`` is
resolved to a set of ``(symbol, event_date)`` pairs by the event resolver in
``event_query.py``:

- ``earnings``               — quarterly earnings announcements (see event_query
                               resolver for the current data-source policy)
- ``opex``                   — US monthly OpEx third Friday
- ``sepa_hit``               — days where the SEPA composite score crossed a threshold
- ``iv_percentile_threshold``— days where IV percentile crossed a threshold
- ``indicator_signal``       — days a standard indicator signal fired on the
                               symbol's daily closes (MACD / RSI / Bollinger /
                               EMA crossings; ``engines.indicators``)
- ``pine_signal``            — days a Pine library script's buy or sell plot
                               fired (``features.stock_signal_pine_daily``)
- ``sql``                    — user-supplied ``SELECT`` returning
                               ``(symbol, event_date)``; not implemented in v1

The engine is D10 BLOCKED — pure historical replay, no order placement path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

EventKind = Literal[
    "earnings",
    "opex",
    "sepa_hit",
    "iv_percentile_threshold",
    "indicator_signal",
    "pine_signal",
    "sql",
]

_ALLOWED_KINDS: frozenset[EventKind] = frozenset(
    ("earnings", "opex", "sepa_hit", "iv_percentile_threshold", "indicator_signal", "pine_signal", "sql")
)


#: Kinds whose event is computed from the session's own close: the signal
#: exists only once that session has closed, so nothing can be filled on it.
SIGNAL_KINDS: frozenset[str] = frozenset(("indicator_signal", "pine_signal"))

#: Version of the entry-timing rule written into every run's summary
#: (``entry_timing``) and the simulator's params (``entry_timing_version``).
#: 1 (before 0.175.0, never written): offset 0 was the event session for every
#: kind, so a signal opened on the close it was computed from. 2: for
#: ``SIGNAL_KINDS`` offset 0 is the first session after the signal session.
ENTRY_TIMING_VERSION = 2


def entry_after_event(kind: str | None) -> bool:
    """True when offsets count from the session after the event (signals)."""
    return kind in SIGNAL_KINDS


def entry_timing(kind: str | None, fill: str) -> dict[str, Any]:
    """The entry rule a run used, as written into its summary.

    ``fill`` names the price the entry session is filled at (``vwap`` /
    ``close`` in the simulator, ``close`` in the event backtest).
    """
    if kind is None:
        anchor, note = "schedule", "opens on the schedule's sessions; no event to look ahead of"
    elif entry_after_event(kind):
        anchor = "session_after_signal"
        note = (
            "a signal is known only at its session's close: offset 0 is the next session, "
            f"filled at that session's {fill}"
        )
    else:
        anchor = "event_session"
        note = f"offset 0 is the first session on or after the event, filled at that session's {fill}"
    return {"version": ENTRY_TIMING_VERSION, "anchor": anchor, "fill": fill, "note": note}


@dataclass(frozen=True)
class EventDef:
    """Named event that anchors a backtest run.

    Params vary per kind — see ``event_query`` for the concrete keys each kind
    consumes. The ``EventDef`` object itself is intentionally permissive to
    keep the API contract flexible; validation lives in the resolver.
    """

    kind: EventKind
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _ALLOWED_KINDS:
            raise ValueError(
                f"invalid EventDef.kind={self.kind!r}; allowed: {sorted(_ALLOWED_KINDS)}"
            )
        if self.params is None:
            object.__setattr__(self, "params", {})

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "params": dict(self.params or {})}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EventDef":
        if not isinstance(data, Mapping):
            raise TypeError(f"EventDef.from_dict expected Mapping, got {type(data)!r}")
        kind = data.get("kind")
        if kind not in _ALLOWED_KINDS:
            raise ValueError(f"invalid kind={kind!r}")
        params = data.get("params") or {}
        if not isinstance(params, Mapping):
            raise TypeError("params must be a mapping")
        return cls(kind=kind, params=dict(params))


__all__ = [
    "ENTRY_TIMING_VERSION",
    "SIGNAL_KINDS",
    "EventDef",
    "EventKind",
    "entry_after_event",
    "entry_timing",
]
