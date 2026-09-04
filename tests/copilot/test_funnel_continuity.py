"""A funnel that skips a cut is not a white-box funnel.

The console read `events 44 -> 44` immediately followed by `option_overlay
24 -> 24`: twenty symbols vanished between two adjacent steps with nothing
accounting for them, because the ranking slice to `limit` happened without a
step. Every drop the resolver makes has to appear as a step, or the funnel is
back to the failure it was built to expose — a number you cannot audit.

The continuity assertion here is deliberately structural rather than a fixed
expected list: it holds for any future layer without needing to be rewritten,
and it fails the moment a new cut is added without recording itself.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.copilot.harness.policy_schema import (
    default_stock_composite_policy,
    parse_policy,
)
from bifrost_research.copilot.harness.universe import composite as composite_mod


def _stub_layers(monkeypatch: Any, *, sepa_symbols: list[str], universe: int) -> None:
    from bifrost_research.copilot.harness.universe import events as events_mod
    from bifrost_research.copilot.harness.universe import momentum as momentum_mod
    from bifrost_research.copilot.harness.universe import sepa as sepa_mod

    monkeypatch.setattr(sepa_mod, "sepa_universe_size", lambda conn: universe)
    monkeypatch.setattr(
        sepa_mod, "fetch_sepa_symbols", lambda conn, **kw: (sepa_symbols, {}, "path IN [SETUP]")
    )
    monkeypatch.setattr(
        momentum_mod, "fetch_momentum_symbols", lambda conn, **kw: ([], {}, "")
    )
    monkeypatch.setattr(events_mod, "fetch_event_symbols", lambda conn, **kw: ([], {}, ""))


def _funnel_for(monkeypatch: Any, *, n_symbols: int, limit: int) -> list[Any]:
    _stub_layers(
        monkeypatch,
        sepa_symbols=[f"SYM{i}" for i in range(n_symbols)],
        universe=3475,
    )
    result = composite_mod.resolve_stock_composite(
        object(), parse_policy(default_stock_composite_policy()), limit=limit
    )
    return list(result.funnel)


def test_no_symbol_disappears_between_adjacent_steps(monkeypatch: Any) -> None:
    """Each step's out_count must be the next step's in_count.

    This is the assertion the console needed and did not have. It is what would
    have caught `events 44 -> 44` sitting next to `option_overlay 24 -> 24`.
    """
    funnel = _funnel_for(monkeypatch, n_symbols=44, limit=24)
    assert len(funnel) >= 2
    for prev, nxt in zip(funnel, funnel[1:]):
        assert prev.out_count == nxt.in_count, (
            f"{prev.name} -> {nxt.name}: {prev.out_count} symbols left "
            f"{prev.name} but {nxt.in_count} entered {nxt.name}"
        )


def test_the_ranking_cut_records_itself(monkeypatch: Any) -> None:
    funnel = _funnel_for(monkeypatch, n_symbols=44, limit=24)
    cut = [s for s in funnel if s.name == "rank_cut"]
    assert cut, "truncating to `limit` must appear as a step"
    assert cut[0].in_count == 44
    assert cut[0].out_count == 24
    # The reader has to be able to tell why 20 were dropped, not just that they were.
    assert "top 24" in cut[0].filter_summary


def test_no_cut_step_when_nothing_was_cut(monkeypatch: Any) -> None:
    """A step that always fires would be noise; a step that never fires is a lie.

    With fewer symbols than the limit the slice is a no-op, and a `44 -> 44`
    row here would be one more thing to read past.
    """
    funnel = _funnel_for(monkeypatch, n_symbols=5, limit=24)
    assert not [s for s in funnel if s.name == "rank_cut"]


def test_the_funnel_still_opens_at_the_universe(monkeypatch: Any) -> None:
    """The new step must not disturb what the first one reports."""
    funnel = _funnel_for(monkeypatch, n_symbols=44, limit=24)
    assert funnel[0].name == "sepa"
    assert funnel[0].in_count == 3475
