"""Slot results say why names failed, and trigger failures are counted (TD-92, TD-113).

``failed += 1`` dropped ``result['error']``: on 2026-10-06 gex failed 42
symbol-days and the run said nothing about which or why; NVR (in the universe since
09-08) and GRML (since 09-24) had had no GEX for weeks without a trace.
"""

from __future__ import annotations

import ast
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from bifrost_research.engines.forecast import playbook as pb
from bifrost_research.scheduler import engines as sched

SCHEDULER = Path(sched.__file__).parent
DAYS = [date(2026, 10, 2), date(2026, 10, 5)]


def _compute(failing: dict[str, str]) -> Any:
    def compute(_conn: Any, *, symbol: str, trade_date: date, **_kw: Any) -> dict[str, Any]:
        if symbol in failing:
            return {"ok": False, "error": failing[symbol]}
        return {"ok": True, "distribution_rows": 10, "rows_written": 10}

    return compute


def _universe(entered: dict[str, date]) -> Any:
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = list(entered.items())
    return conn


def test_gex_failures_carry_reasons_and_the_persistent_names() -> None:
    conn = _universe({"NVR": date(2026, 9, 8), "NEWCO": date(2026, 10, 1)})
    failing = {"NVR": "No OI contracts", "NEWCO": "No OI contracts", "QRVO": "No spot price"}
    with patch.object(sched, "compute_gex_for_symbol", side_effect=_compute(failing)):
        result = sched.run_gex(conn, trading_days=DAYS, symbols=["AAPL", "NEWCO", "NVR", "QRVO"])
    assert result["symbols_failed"] == 6 and result["symbols_ok"] == 2
    assert result["failures_by_reason"] == {"No OI contracts": 4, "No spot price": 2}
    assert result["failed_names_by_reason"]["No OI contracts"] == "NEWCO, NVR"
    assert result["failed_every_session"] == 3
    # NEWCO entered 4 days before the session: still onboarding. QRVO is not in
    # the universe table (a watchlist name) and counts as onboarded.
    assert result["failed_every_session_past_onboarding"] == ["NVR", "QRVO"]


def test_an_unreadable_universe_says_onboarding_is_unknown() -> None:
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("down")
    with patch.object(
        sched, "compute_order_flow_for_symbol", side_effect=_compute({"NVR": "No option tape or OI/snapshot rows"})
    ):
        result = sched.run_flow(conn, trading_days=DAYS, symbols=["NVR"])
    assert result["failed_every_session_past_onboarding"] is None
    assert "unknown" in result["onboarding"]


def test_every_slot_summary_with_symbols_failed_carries_reasons() -> None:
    """Ratchet: a dict literal in scheduler/ that reports ``symbols_failed`` must also
    carry ``failures_by_reason`` (or spread ``failure_summary(...)``)."""
    offenders: list[str] = []
    for path in sorted(SCHEDULER.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if "symbols_failed" not in keys:
                continue
            spreads = [
                v
                for k, v in zip(node.keys, node.values, strict=True)
                if k is None and isinstance(v, ast.Call) and getattr(v.func, "id", "") == "failure_summary"
            ]
            if "failures_by_reason" not in keys and not spreads:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"symbols_failed without failures_by_reason: {offenders}"


def test_bare_failure_counters_in_scheduler_only_fall() -> None:
    """``failed += 1`` drops the reason. Baseline 0 since TD-92."""
    count = sum(
        1
        for path in SCHEDULER.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.AugAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "failed"
        and isinstance(node.op, ast.Add)
    )
    assert count == 0


# --- TD-113: trigger emission -------------------------------------------------


def test_a_failed_previous_state_lookup_emits_no_snapshot(monkeypatch: Any) -> None:
    written: list[Any] = []
    monkeypatch.setattr(pb, "upsert_playbook_triggers", lambda conn, events: written.extend(events) or len(events))
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("permission denied")
    try:
        pb.emit_triggers_for_terrain_intraday(
            conn,
            symbol="PLTR",
            trade_date=date(2026, 10, 5),
            asof_ts=datetime(2026, 10, 5, 15, tzinfo=timezone.utc),
            regime="range",
            prob_rangy=0.6,
            prob_bull=0.2,
            prob_bear=0.1,
            prob_squeeze=0.1,
        )
    except pb.TriggerStateUnavailable as exc:
        assert "PLTR" in str(exc)
    else:
        raise AssertionError("a failed lookup must not compare against nothing")
    assert written == []


def test_guarded_emission_logs_and_counts(caplog: Any) -> None:
    tally = pb.TriggerTally()
    conn = MagicMock()

    def broken() -> None:
        raise pb.TriggerStateUnavailable("previous trigger state of PLTR: relation does not exist")

    pb.emit_triggers_guarded(conn, broken, symbol="PLTR", tally=tally)
    pb.emit_triggers_guarded(conn, lambda: 1, symbol="AAPL", tally=tally)
    assert tally.summary()["trigger_ok"] == 1 and tally.summary()["trigger_failures"] == 1
    assert "PLTR" in tally.summary()["trigger_failure_sample"]
    assert conn.rollback.called
    assert any("PLTR" in r.getMessage() for r in caplog.records)


def test_terrain_intraday_reports_trigger_failures() -> None:
    with patch.object(sched, "ny_today", return_value=date(2026, 10, 5)), \
         patch.object(sched, "_intraday_chain_symbols", return_value=[]), \
         patch.object(sched, "load_upstream_signals", return_value=(192.59, {}, {}, {})), \
         patch("bifrost_research.engines.forecast.terrain.upsert_terrain_intraday", return_value=1), \
         patch(
             "bifrost_research.engines.forecast.playbook.emit_triggers_for_terrain_intraday",
             side_effect=pb.TriggerStateUnavailable("down"),
         ):
        result = sched.run_terrain_intraday(MagicMock(), trading_days=[], symbols=["PLTR", "AAPL"])
    assert result["trigger_failures"] == 2 and result["trigger_ok"] == 0
