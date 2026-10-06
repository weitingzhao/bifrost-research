"""Indicator signal sessions for signal statistics (signal evaluation).

``GET /research/indicators/signal-stats`` computes each symbol's crossing
sessions here from its adjusted daily closes, then measures them with
``engines.signal_stats.evaluate`` — the method Pine signals use (0.175.0):
next-open entry, a one-way cost charged each way, a per-horizon cooldown,
direction-signed returns, a non-signal baseline and a 90% cluster bootstrap.
Until 0.179.0 this module entered on the crossing session's own close, gross,
with overlapping signals and undirected averages.

A crossing inside a symbol's first ``warmup_bars`` loaded bars is dropped: the
indicator has not settled there (a name listed after the warm-up started).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from bifrost_research.engines.indicators.compute import get_signal, signal_mask
from bifrost_research.repositories.listing_lineage import live_label


def signal_sessions(
    series: Mapping[str, tuple[Sequence[date], Sequence[float]]],
    signal_id: str,
    params: Mapping[str, Any] | None,
    start: date,
    end: date,
    *,
    warmup_bars: int = 0,
) -> dict[str, list[date]]:
    """Crossing sessions in [start, end] per symbol (live label), past the warm-up."""
    spec = get_signal(signal_id)
    p = spec.params(params)
    out: dict[str, list[date]] = {}
    for sym, (dates, closes) in series.items():
        mask = signal_mask(closes, spec.id, p)
        hits = [d for i, d in enumerate(dates) if mask[i] and i >= warmup_bars and start <= d <= end]
        out.setdefault(live_label(sym), []).extend(hits)
    return out


def sign_of(signal_id: str) -> int:
    """+1 for a signal that expects a rise, -1 for one that expects a fall."""
    return 1 if get_signal(signal_id).direction == "up" else -1


__all__ = ["sign_of", "signal_sessions"]
