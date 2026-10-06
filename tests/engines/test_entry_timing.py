"""Entry timing for event entries: a signal kind enters the session after it (0.175.0, 0.176.0)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest


def test_signal_windows_count_from_the_session_after_the_signal() -> None:
    from bifrost_research.engines.backtest.event_defs import entry_after_event, entry_timing
    from bifrost_research.engines.backtest.event_query import _clip_to_listing_end
    from bifrost_research.engines.backtest.strategy_templates import LegSpec, resolve_trading_window

    days = [date(2026, 3, 2) + timedelta(days=i) for i in range(5)]  # Mon..Fri
    leg = LegSpec(kind="stock", side="buy", entry_offset_days=0, exit_offset_days=2)
    assert resolve_trading_window(leg, days[1], days) == (days[1], days[3])
    assert resolve_trading_window(leg, days[1], days, after_event=True) == (days[2], days[4])
    # A Friday signal with no later session in view has nothing to enter on.
    assert resolve_trading_window(leg, days[4], days, after_event=True) is None
    assert _clip_to_listing_end(leg, days[2], days, after_event=True) == (days[3], days[4])
    assert entry_after_event("pine_signal") and entry_after_event("indicator_signal")
    assert not entry_after_event("earnings") and not entry_after_event("opex")
    assert entry_timing("pine_signal", "close")["anchor"] == "session_after_signal"
    assert entry_timing("earnings", "close")["anchor"] == "event_session"
    assert entry_timing(None, "vwap") == {
        "version": 3,
        "anchor": "schedule",
        "fill": "vwap",
        "note": "opens on the schedule's sessions; no event to look ahead of",
    }



def test_sepa_and_iv_percentile_are_signals_and_default_to_the_next_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from bifrost_research.engines.backtest import event_query as eq
    from bifrost_research.engines.backtest.event_defs import (
        SIGNAL_KINDS,
        check_entry_offset,
        default_entry_offset,
        entry_timing,
    )

    assert {"sepa_hit", "iv_percentile_threshold", "pine_signal", "indicator_signal"} == set(SIGNAL_KINDS)
    assert default_entry_offset("sepa_hit") == 0 and default_entry_offset("earnings") == -1
    assert entry_timing("iv_percentile_threshold", "close")["anchor"] == "session_after_signal"
    check_entry_offset("earnings", -1)  # a dated event may be entered the session before
    with pytest.raises(ValueError, match="sepa_hit"):
        check_entry_offset("sepa_hit", -1)

    class _NoReads:
        def cursor(self) -> None:
            raise AssertionError("refused before any read")

    # Negative offsets are refused for every caller, the MCP tool included.
    with pytest.raises(ValueError, match="must be 0 or later"):
        eq.run_event_query({"kind": "sepa_hit"}, "long_stock_event", conn=_NoReads(), entry_offset_days=-1)
    with pytest.raises(ValueError, match="must be 0 or later"):
        eq.run_event_query({"kind": "iv_percentile_threshold"}, "long_stock_event", conn=_NoReads(), entry_offset_days=0, exit_offset_days=-2)

    monkeypatch.setattr(eq, "resolve_events", lambda conn, ed, lb, today=None: eq.ResolvedEvents(events=[], source="sepa"))
    out = eq.run_event_query({"kind": "sepa_hit"}, "long_stock_event", conn=_NoReads())
    assert out["template_kwargs"]["entry_offset_days"] == 0
    assert out["summary"]["entry_timing"]["anchor"] == "session_after_signal"
    out = eq.run_event_query({"kind": "earnings"}, "long_stock_event", conn=_NoReads())
    assert "entry_offset_days" not in out["template_kwargs"]  # the template's -1 stands
    assert out["summary"]["entry_timing"]["anchor"] == "event_session"
