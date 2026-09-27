"""Playbook triggers: a session logs what changed, once (2026-09-26).

Since session ids became symbol-and-date (0.126.0) every call logged a fresh
snapshot, and the nightly recomputation of the last two dates logged another
one stamped the night after.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Self

import pytest

from bifrost_research.engines.forecast import playbook as pb
from bifrost_research.engines.forecast.playbook import ForecastSession, ScenarioProbabilities

D = date(2026, 9, 24)


def _session(rangy: float, bull: float) -> ForecastSession:
    return ForecastSession(
        session_id=f"PLTR-{D.isoformat()}",
        symbol="PLTR",
        trade_date=D,
        regime="range",
        spot=192.59,
        scenarios=ScenarioProbabilities(rangy=rangy, bull=bull, bear=0.1, squeeze=0.1),
        expected_close=188.65,
    )


class _Cur:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.sql.append(sql)

    def fetchone(self) -> Any:
        return self.conn.row


class _Conn:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.sql: list[str] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def rollback(self) -> None:
        return None


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    monkeypatch.setattr(pb, "upsert_playbook_triggers", lambda conn, events: out.extend(events) or len(events))
    return out


def test_the_same_state_as_the_log_writes_nothing(written: list[dict[str, Any]]) -> None:
    s = _session(0.6, 0.2)
    logged = s.scenarios.normalized()
    conn = _Conn(({"rangy": logged.rangy, "bull": logged.bull, "bear": logged.bear, "squeeze": logged.squeeze},))
    pb.emit_triggers_for_session(conn, s)
    assert written == []
    assert "stock_signal_playbook_trigger_intraday" in conn.sql[0], "the log is the previous state"


def test_a_changed_dominant_is_one_change_event(written: list[dict[str, Any]]) -> None:
    conn = _Conn(({"rangy": 0.2, "bull": 0.6, "bear": 0.1, "squeeze": 0.1},))
    pb.emit_triggers_for_session(conn, _session(0.6, 0.2))
    events = {e["condition_snapshot"]["event"] for e in written}
    assert "snapshot" not in events
    assert any(e["scenario_key"] == "dominant:rangy" and e["condition_snapshot"]["from"] == "bull" for e in written)


def test_an_empty_log_is_a_snapshot(written: list[dict[str, Any]]) -> None:
    pb.emit_triggers_for_session(_Conn(None), _session(0.6, 0.2))
    assert written[0]["condition_snapshot"]["event"] == "snapshot"


@pytest.mark.parametrize(("existed", "emits"), [(True, False), (False, True)])
def test_a_recomputed_session_writes_no_trigger(
    monkeypatch: pytest.MonkeyPatch, existed: bool, emits: bool
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(pb, "_session_exists", lambda conn, sid: existed)
    monkeypatch.setattr(pb, "batch_upsert", lambda *a, **k: 1)
    monkeypatch.setattr(pb, "upsert_hourly_sessions", lambda conn, s: None)
    monkeypatch.setattr(pb, "emit_triggers_for_session", lambda conn, s: calls.append(s.session_id))
    pb.upsert_forecast_session(_Conn(None), _session(0.6, 0.2))
    assert bool(calls) is emits


def test_a_failed_lookup_counts_as_existing() -> None:
    class _Broken(_Conn):
        def cursor(self) -> _Cur:
            raise RuntimeError("down")

    assert pb._session_exists(_Broken(None), "PLTR-2026-09-24") is True
    assert pb._session_exists(_Conn((1,)), "x") is True
    assert pb._session_exists(_Conn(None), "x") is False
