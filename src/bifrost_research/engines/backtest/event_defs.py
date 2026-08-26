"""Event definitions for the event-driven backtest query engine (Wave RS-C1).

An ``EventDef`` names *when* to run a strategy template. Each ``kind`` is
resolved to a set of ``(symbol, event_date)`` pairs by the event resolver in
``event_query.py``:

- ``earnings``               — quarterly earnings announcements (see event_query
                               resolver for the current data-source policy)
- ``opex``                   — US monthly OpEx third Friday
- ``sepa_hit``               — days where the SEPA composite score crossed a threshold
- ``iv_percentile_threshold``— days where IV percentile crossed a threshold
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
    "sql",
]

_ALLOWED_KINDS: frozenset[EventKind] = frozenset(
    ("earnings", "opex", "sepa_hit", "iv_percentile_threshold", "sql")
)


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


__all__ = ["EventDef", "EventKind"]
