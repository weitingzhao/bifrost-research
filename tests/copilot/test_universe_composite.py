"""LS-2 universe adapter unit tests (mock conn)."""

from __future__ import annotations

from unittest.mock import MagicMock

from bifrost_research.copilot.harness.policy_schema import (
    EventsLayerPolicy,
    LoopPolicy,
    MomentumLayerPolicy,
    OptionOverlayPolicy,
    SepaLayerPolicy,
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
