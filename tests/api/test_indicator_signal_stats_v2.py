"""GET /research/indicators/signal-stats on the 0.179.0 method (engines.signal_stats)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bifrost_research.api import indicators as api
from bifrost_research.api.app import create_app


@pytest.fixture(autouse=True)
def _health_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bifrost_research.api.health.run_startup_schema_guard", lambda: None)


def test_crossings_are_measured_by_the_shared_method(monkeypatch: pytest.MonkeyPatch) -> None:
    closes = [70 + 2 * i for i in range(30)] + [128 - 3 * i for i in range(30)]  # up, then a cross down
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(len(closes))]
    bars = [{"date": d, "close": float(c)} for d, c in zip(days, closes)]
    calls: list[dict[str, Any]] = []

    def fake_evaluate(conn: Any, by_sym: Any, **kw: Any) -> dict[str, Any]:
        calls.append({"by_sym": by_sym, **kw})
        d = by_sym["AAA"][0]
        return {
            "signals": 1,
            "sample_note": "noise",
            "method": {"version": 2},
            "by_horizon": {"5": {"win_rate_edge": 0.1}},
            "per_symbol": {},
            "recent": [{"symbol": "AAA", "date": d.isoformat(), "ret_5": 0.03, "counted_5": True}],
        }

    monkeypatch.setattr(api, "connect", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr(api, "load_bars", lambda conn, sym, s, e, *, warmup_sessions=0: bars)
    monkeypatch.setattr(api, "evaluate", fake_evaluate)
    resp = TestClient(create_app()).get(
        "/research/indicators/signal-stats",
        params={"signal": "close_ema_cross_down", "symbols": "aaa", "params": '{"length": 5}', "start": "2025-01-01",
                "end": "2025-03-01", "horizons": "5", "cost_bps": 15},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    kw = calls[0]
    assert kw["sign"] == -1 and kw["cost_bps"] == 15 and kw["detail"] is True and kw["horizons"] == [5]
    assert data["method"]["version"] == 2 and data["cost_bps"] == 15 and data["signal"]["direction"] == "down"
    assert data["recent"][0]["close"] == next(b["close"] for b in bars if b["date"].isoformat() == data["recent"][0]["date"])
