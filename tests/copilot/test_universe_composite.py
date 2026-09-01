"""LS-2 universe adapter unit tests (mock conn)."""

from __future__ import annotations

from typing import Any

from unittest.mock import MagicMock

from bifrost_research.copilot.harness.policy_schema import (
    EventsLayerPolicy,
    LoopPolicy,
    MomentumLayerPolicy,
    OptionOverlayPolicy,
    SepaLayerPolicy,
)
from bifrost_research.copilot.harness.policy_schema import (
    default_stock_composite_policy,
    parse_policy,
)
from bifrost_research.copilot.harness.universe import composite as composite_mod
from bifrost_research.copilot.harness.universe import option_overlay as overlay_mod


def _mock_conn(fetchall_rows: list[tuple], description: list[str]) -> MagicMock:
    cur = MagicMock()
    cur.description = [(c,) for c in description]
    cur.fetchall.return_value = fetchall_rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


def test_apply_layer_intersect_required_empty() -> None:
    out, step = composite_mod._apply_layer(
        name="momentum",
        current=["AAPL", "MSFT"],
        layer_symbols=[],
        required=True,
        filter_summary="test",
    )
    assert out == []
    assert step.out_count == 0


def test_apply_layer_optional_empty_keeps_current() -> None:
    out, step = composite_mod._apply_layer(
        name="events",
        current=["AAPL"],
        layer_symbols=[],
        required=False,
        filter_summary="test",
    )
    assert out == ["AAPL"]
    assert step.skipped is True


def test_option_overlay_not_required_keeps_missing_scan() -> None:
    conn = MagicMock()
    policy = LoopPolicy(
        universe_mode="stock_composite",
        option_overlay=OptionOverlayPolicy(enabled=True, required=False),
    )
    symbols = ["AAPL", "MSFT"]
    meta = {"AAPL": {"sepa_score": 80}, "MSFT": {"sepa_score": 75}}

    from bifrost_research.copilot.harness import data_sources as ds

    original = ds.top_scan_symbols
    try:
        ds.top_scan_symbols = lambda *a, **k: [{"symbol": "AAPL", "composite_score": 0.9}]
        kept, merged, step, applied = overlay_mod.apply_option_overlay(
            conn,
            symbols=symbols,
            row_meta=meta,
            overlay=policy.option_overlay,
            policy=policy,
        )
    finally:
        ds.top_scan_symbols = original

    assert "MSFT" in kept
    assert "AAPL" in kept
    assert applied is True
    assert step is not None


def test_apply_layer_optional_non_overlapping_keeps_current() -> None:
    """`required: false` must survive a layer that has rows but shares none.

    The empty-layer case was already covered; this one was not, and it is the
    one that fired in production: SEPA produced 47 candidates and the optional
    events layer — holding a single unrelated symbol — cut them to zero.
    """
    out, step = composite_mod._apply_layer(
        name="events",
        current=["AAPL", "MSFT"],
        layer_symbols=["ZZZZ"],
        required=False,
        filter_summary="test",
    )
    assert out == ["AAPL", "MSFT"]
    assert step.skipped is True
    assert step.skip_reason == "no overlap with upstream layers"
    assert step.out_count == 2


def test_apply_layer_required_non_overlapping_still_empties() -> None:
    """The relaxation above must not leak into required layers."""
    out, step = composite_mod._apply_layer(
        name="sepa",
        current=["AAPL", "MSFT"],
        layer_symbols=["ZZZZ"],
        required=True,
        filter_summary="test",
    )
    assert out == []
    assert step.out_count == 0
    assert step.skipped is False


def test_apply_layer_optional_partial_overlap_still_narrows() -> None:
    """Optional means "may not empty", not "may not filter"."""
    out, step = composite_mod._apply_layer(
        name="momentum",
        current=["AAPL", "MSFT", "NVDA"],
        layer_symbols=["MSFT", "NVDA", "ZZZZ"],
        required=False,
        filter_summary="test",
    )
    assert out == ["MSFT", "NVDA"]
    assert step.skipped is False
    assert step.out_count == 2


def test_composite_funnel_opens_at_the_universe_not_at_sepa_output(
    monkeypatch: Any,
) -> None:
    """`3472 -> 47` is a screen; `47 -> 47` is not evidence of one.

    SEPA's path and score filters run inside its query, so the funnel used to
    open at whatever came back. Every composite run then read as watchlist-sized
    on the console — the exact confusion the funnel exists to prevent.
    """
    from bifrost_research.copilot.harness.universe import sepa as sepa_mod

    monkeypatch.setattr(sepa_mod, "sepa_universe_size", lambda conn: 3472)
    monkeypatch.setattr(
        sepa_mod,
        "fetch_sepa_symbols",
        lambda conn, **kw: (["AAPL", "MSFT"], {}, "path IN [SETUP]"),
    )
    from bifrost_research.copilot.harness.universe import events as events_mod
    from bifrost_research.copilot.harness.universe import momentum as momentum_mod

    monkeypatch.setattr(
        momentum_mod, "fetch_momentum_symbols", lambda conn, **kw: ([], {}, "")
    )
    monkeypatch.setattr(events_mod, "fetch_event_symbols", lambda conn, **kw: ([], {}, ""))

    result = composite_mod.resolve_stock_composite(
        object(), parse_policy(default_stock_composite_policy()), limit=8
    )
    first = result.funnel[0]
    assert first.name == "sepa"
    assert first.in_count == 3472
    assert first.out_count == 2


def test_composite_funnel_falls_back_when_the_universe_is_unreadable(
    monkeypatch: Any,
) -> None:
    """An unreadable count must not report the universe as zero."""
    from bifrost_research.copilot.harness.universe import sepa as sepa_mod

    monkeypatch.setattr(sepa_mod, "sepa_universe_size", lambda conn: None)
    monkeypatch.setattr(
        sepa_mod, "fetch_sepa_symbols", lambda conn, **kw: (["AAPL"], {}, "f")
    )
    from bifrost_research.copilot.harness.universe import events as events_mod
    from bifrost_research.copilot.harness.universe import momentum as momentum_mod

    monkeypatch.setattr(
        momentum_mod, "fetch_momentum_symbols", lambda conn, **kw: ([], {}, "")
    )
    monkeypatch.setattr(events_mod, "fetch_event_symbols", lambda conn, **kw: ([], {}, ""))

    result = composite_mod.resolve_stock_composite(
        object(), parse_policy(default_stock_composite_policy()), limit=8
    )
    assert result.funnel[0].in_count == 1
