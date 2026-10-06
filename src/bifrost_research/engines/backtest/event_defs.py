"""Event definitions for the event-driven backtest query engine (Wave RS-C1).

An ``EventDef`` names *when* to run a strategy template. Each ``kind`` is
resolved to a set of ``(symbol, event_date)`` pairs by the event resolver in
``event_query.py``:

- ``earnings``               — quarterly earnings announcements (see event_query
                               resolver for the current data-source policy)
- ``opex``                   — US monthly OpEx third Friday
- ``sepa_hit``               — days where the SEPA composite score crossed a threshold
                               (scored on the session's close)
- ``iv_percentile_threshold``— days where IV percentile crossed a threshold
                               (from the session's end-of-day option data)
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
#: An indicator or Pine plot is read off the day's bar; a SEPA score is scored
#: on the day's close; an IV percentile comes from the day's end-of-day option
#: data. An earnings date or an OpEx Friday is known before the session opens.
SIGNAL_KINDS: frozenset[str] = frozenset(("indicator_signal", "pine_signal", "sepa_hit", "iv_percentile_threshold"))

#: Version of the entry-timing rule written into every run's summary
#: (``entry_timing``) and the simulator's params (``entry_timing_version``).
#: 1 (before 0.175.0, never written): offset 0 was the event session for every
#: kind, so a signal opened on the close it was computed from. 2 (0.175.0): for
#: indicator and Pine signals offset 0 is the first session after the signal
#: session. 3 (0.176.0): the same for ``sepa_hit`` and
#: ``iv_percentile_threshold``, and a signal kind with no offset given enters
#: on offset 0 instead of the templates' and simulator's -1.
ENTRY_TIMING_VERSION = 3


def entry_after_event(kind: str | None) -> bool:
    """True when offsets count from the session after the event (signals)."""
    return kind in SIGNAL_KINDS


def default_entry_offset(kind: str | None) -> int:
    """The entry offset when the caller gives none: the next session for a signal,
    the session before for a dated event (the pre-event entry templates assume)."""
    return 0 if entry_after_event(kind) else -1


def check_entry_offset(kind: str | None, offset: int | float) -> None:
    """Refuse a negative offset on a signal kind: it would fill on or before the
    close the signal was computed from."""
    if entry_after_event(kind) and offset < 0:
        raise ValueError(
            f"a {kind} event is known only at its session's close: the entry offset counts "
            "from the next session (0 = the session after the signal) and must be 0 or later"
        )


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
    "check_entry_offset",
    "default_entry_offset",
    "entry_after_event",
    "entry_timing",
]
