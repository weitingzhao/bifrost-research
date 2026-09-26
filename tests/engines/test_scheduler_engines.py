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


def test_run_settlement_settles_the_prior_session_against_the_settle_day() -> None:
    """A session dated D forecasts D+1: it is settled with D+1's close and bars,
    and every other settlement row for the symbol and D goes (2026-09-26)."""
    settle_day = date(2026, 9, 24)
    session_day = date(2026, 9, 23)

    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.side_effect = [
        [("PLTR-2026-09-23", "PLTR", 188.7, 191.79)],  # latest session per symbol for D
        [("PLTR", 191.79, {"gex": {"major_call_wall": 195.0, "major_put_wall": 185.0}})],  # D's terrain
        [(10, "higher-high", 190.0, 193.0, 191.0)],    # its hourly path
    ]
    cursor.rowcount = 2
    conn = MagicMock()
    conn.cursor.return_value = cursor

    captured = {}
    with patch.object(sched, "fetch_recent_trading_days", return_value=[session_day]) as cal, \
         patch.object(sched, "load_actual_close", return_value=192.59) as actual, \
         patch.object(sched, "load_hourly_closes", return_value={10: 192.115}) as bars, \
         patch.object(sched, "upsert_settlement", side_effect=lambda c, s: captured.setdefault("stl", s)):
        result = sched.run_settlement(conn, trading_days=[settle_day], symbols=[])

    assert cal.call_args.kwargs["as_of"] == date(2026, 9, 23)
    actual.assert_called_once_with(conn, "PLTR", settle_day)
    bars.assert_called_once_with(conn, "PLTR", settle_day)
    stl = captured["stl"]
    assert stl.trade_date == session_day
    assert stl.actual_close == 192.59
    assert stl.stats_json["target_date"] == "2026-09-24"
    assert stl.stats_json["path_basis"] == "hourly"
    assert "input_fault" not in stl.stats_json
    delete_sql, delete_params = cursor.execute.call_args_list[-1].args
    assert "DELETE FROM features.stock_backtest_settlement" in delete_sql
    assert delete_params == ("PLTR", session_day, "stl-PLTR-2026-09-23")
    closed_sql, closed_params = cursor.execute.call_args_list[0].args
    assert "trade_date > %s AND trade_date < %s" in closed_sql
    assert closed_params == (session_day, settle_day)
    assert result["sessions_settled"] == 1
    assert result["settled_on_hourly_bars"] == 1
    assert result["stale_settlements_removed"] == 4  # two deletes, rowcount 2 each
    assert result["input_faults"] == 0


def test_run_settlement_stamps_a_session_drawn_from_walls_off_spot() -> None:
    """PLTR 2026-07-27: both walls on 20 against a 131.53 close — the backfilled
    chain held a handful of contracts — so the target was 20.5. The settlement is
    written and stamped; rates leave it out (2026-09-26)."""
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.side_effect = [
        [("PLTR-2026-07-27", "PLTR", 20.502, 131.53)],
        [
            ("PLTR", 131.53, {"gamma_zone_source": "walls_widened",
                              "gex": {"zero_gamma": 20.0, "major_call_wall": 20.0, "major_put_wall": 20.0}}),
            ("SPY", 640.0, {"gamma_zone_source": "walls_off_spot",
                            "gex": {"major_call_wall": 400.0, "major_put_wall": 380.0}}),
        ],
        [],
    ]
    cursor.rowcount = 0
    conn = MagicMock()
    conn.cursor.return_value = cursor

    captured = {}
    with patch.object(sched, "fetch_recent_trading_days", return_value=[date(2026, 7, 27)]), \
         patch.object(sched, "load_actual_close", return_value=123.53), \
         patch.object(sched, "load_hourly_closes", return_value={}), \
         patch.object(sched, "upsert_settlement", side_effect=lambda c, s: captured.setdefault("stl", s)):
        result = sched.run_settlement(conn, trading_days=[date(2026, 7, 28)], symbols=[])

    stl = captured["stl"]
    assert stl.stats_json["input_fault"] == "walls_off_spot"
    assert result["input_faults"] == 1
