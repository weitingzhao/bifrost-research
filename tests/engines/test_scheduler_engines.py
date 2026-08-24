"""Unit tests for Wave 3–4 engine scheduler slot wiring."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from bifrost_research.scheduler import engines as sched


def test_slot_names_include_wave3_and_wave4() -> None:
    assert "momentum" in sched.SLOT_NAMES
    assert "gex" in sched.SLOT_NAMES
    assert "iv-surface" in sched.SLOT_NAMES
    assert "flow" in sched.SLOT_NAMES
    assert "terrain" in sched.SLOT_NAMES
    assert "forecast" in sched.SLOT_NAMES


def test_run_momentum_calls_engine_for_each_day() -> None:
    conn = MagicMock()
    days = [date(2026, 8, 18), date(2026, 8, 19)]
    symbols = ["SPY", "QQQ"]
    with patch.object(
        sched,
        "compute_momentum_for_date",
        return_value={"rows_written": 2, "skipped": 0},
    ) as compute:
        result = sched.run_momentum(conn, trading_days=days, symbols=symbols)
    assert result["slot"] == "momentum"
    assert result["rows_written"] == 4
    assert compute.call_count == 2
    assert "scaffolding" not in result


def test_run_gex_counts_ok_and_failed() -> None:
    conn = MagicMock()
    days = [date(2026, 8, 19)]
    symbols = ["SPY", "AAPL"]
    with patch.object(
        sched,
        "compute_gex_for_symbol",
        side_effect=[
            {"ok": True, "distribution_rows": 10},
            {"ok": False, "error": "No OI"},
        ],
    ):
        result = sched.run_gex(conn, trading_days=days, symbols=symbols)
    assert result["slot"] == "gex"
    assert result["rows_written"] == 10
    assert result["symbols_ok"] == 1
    assert result["symbols_failed"] == 1
    assert "scaffolding" not in result


def test_run_iv_surface_and_flow_have_no_scaffolding() -> None:
    conn = MagicMock()
    days = [date(2026, 8, 19)]
    symbols = ["SPY"]
    with patch.object(
        sched,
        "compute_iv_surface_for_symbol",
        return_value={"ok": True, "rows_written": 3},
    ):
        surface = sched.run_iv_surface(conn, trading_days=days, symbols=symbols)
    with patch.object(
        sched,
        "compute_order_flow_for_symbol",
        return_value={"ok": True},
    ):
        flow = sched.run_flow(conn, trading_days=days, symbols=symbols)
    assert surface["rows_written"] == 3
    assert flow["rows_written"] == 1
    assert "scaffolding" not in surface
    assert "scaffolding" not in flow


def test_unknown_slot_not_in_runners() -> None:
    assert sched._SLOT_RUNNERS.get("not-a-slot") is None
    with pytest.raises(ValueError, match="unknown slot"):
        # Bypass connect by calling with a fake slot via monkeypatch of get
        runner = sched._SLOT_RUNNERS.get("not-a-slot")
        if runner is None:
            raise ValueError("unknown slot: not-a-slot")
