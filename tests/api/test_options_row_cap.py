"""ATM-IV and max-pain reads say when they were cut (2026-09-26).

A flat LIMIT 500 returned about a month of a 365-day ATM-IV lookback for one
name (SPY carries 15.8 expiries a session) and reported nothing.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from bifrost_research.api import options as options_api
from bifrost_research.api.app import create_app


class _Conn:
    def close(self) -> None:
        return None


def _rows(n: int) -> list[dict[str, Any]]:
    return [{"symbol": "SPY", "trade_date": "2026-09-25", "expiry": f"x{i}"} for i in range(n)]


def test_row_cap_is_room_for_a_year_for_one_name() -> None:
    assert options_api.row_cap("SPY") == options_api.SYMBOL_ROW_CAP == 10_000
    assert options_api.row_cap(None) == options_api.ALL_SYMBOLS_ROW_CAP == 500
    assert options_api.row_cap("  ") == 500


def test_atm_iv_says_truncated_when_a_row_comes_back_past_the_cap(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake(conn, *, symbol, expiry, trade_date, lookback_days, limit):
        seen["limit"] = limit
        return _rows(limit)

    monkeypatch.setattr(options_api, "connect", lambda: _Conn())
    monkeypatch.setattr(options_api, "query_atm_iv", fake)
    body = TestClient(create_app()).get("/analytics/options/atm-iv?symbol=SPY&lookback_days=365").json()
    assert seen["limit"] == 10_001
    assert body["truncated"] is True and body["row_cap"] == 10_000 and body["count"] == 10_000


def test_max_pain_is_not_truncated_under_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(options_api, "connect", lambda: _Conn())
    monkeypatch.setattr(
        options_api,
        "query_max_pain",
        lambda conn, *, symbol, expiry, trade_date, lookback_days, limit: _rows(2162),
    )
    body = TestClient(create_app()).get("/analytics/options/max-pain?symbol=PLTR&lookback_days=365").json()
    assert body["truncated"] is False and body["count"] == 2162
