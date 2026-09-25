"""Earnings moves: print dates, the single-session print, the straddle, the crush."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from fastapi.testclient import TestClient

from bifrost_research.api import risk_stats
from bifrost_research.api.app import create_app
from bifrost_research.engines.volatility.earnings_moves import one_print, print_dates, straddle_move, summarize


def test_amendments_within_a_week_are_the_same_print() -> None:
    d = [date(2026, 8, 3), date(2026, 8, 5), date(2026, 5, 4), date(2026, 2, 2)]
    assert print_dates(d, limit=8) == [date(2026, 2, 2), date(2026, 5, 4), date(2026, 8, 3)]
    assert print_dates(d, limit=2) == [date(2026, 5, 4), date(2026, 8, 3)]


def test_an_at_the_money_straddle_is_about_point_eight_sigma_root_t() -> None:
    move = straddle_move(100.0, 100.0, 0.50, 7)
    assert move is not None and abs(move - 0.798 * 0.50 * math.sqrt(7 / 365)) < 2e-3
    assert straddle_move(100.0, 100.0, 0.50, 0) is None


# Invented closes shaped like NVDA's 2025-02 print: up into the close, down after it.
CLOSES = {date(2031, 2, 24): 100.0, date(2031, 2, 25): 103.7, date(2031, 2, 26): 94.9}
ATM = {
    date(2031, 2, 24): [(date(2031, 2, 28), 100.0, 0.80), (date(2031, 3, 7), 100.0, 0.70)],
    date(2031, 2, 26): [(date(2031, 3, 7), 95.0, 0.45)],
}


def test_the_print_is_the_larger_session_not_the_two_day_change() -> None:
    row = one_print(date(2031, 2, 25), CLOSES, ATM)
    assert row["before"] == "2031-02-24" and row["after"] == "2031-02-26"
    assert row["direction"] == "down"
    assert abs(row["actual"] - (1 - 94.9 / 103.7)) < 1e-6  # not 1 − 94.9/100
    assert row["expiry"] == "2031-02-28"  # the front expiry covering the window prices it
    assert abs(row["priced"] - straddle_move(100.0, 100.0, 0.80, 4)) < 1e-6
    assert abs(row["ratio"] - row["actual"] / row["priced"]) < 1e-3


def test_the_crush_uses_the_first_expiry_both_sessions_price() -> None:
    row = one_print(date(2031, 2, 25), CLOSES, ATM)
    assert row["crush_expiry"] == "2031-03-07"
    assert row["crush_pts"] == -25.0


def test_a_weekend_filing_reads_the_one_session_around_it() -> None:
    closes = {date(2031, 3, 7): 100.0, date(2031, 3, 10): 110.0}
    row = one_print(date(2031, 3, 8), closes, {})
    assert row["actual"] == 0.1 and row["direction"] == "up"
    assert row["priced"] is None and row["missing"] == "no ATM IV for an expiry covering the print"


def test_a_print_with_no_close_after_it_says_so() -> None:
    row = one_print(date(2031, 3, 10), {date(2031, 3, 9): 100.0}, {})
    assert row["actual"] is None and row["missing"] == "no close after the print yet"


def test_rich_counts_moves_under_what_was_priced() -> None:
    rows: list[dict[str, Any]] = [{"ratio": 0.5}, {"ratio": 1.4}, {"ratio": 0.9}, {"ratio": None}]
    assert summarize(rows) == {"n": 3, "median_ratio": 0.9, "rich": 2}


def test_the_route_answers_in_the_envelope(monkeypatch) -> None:
    monkeypatch.setattr("bifrost_research.api.health.run_startup_schema_guard", lambda: None)

    class _C:
        def close(self) -> None:
            return None

    seen: dict[str, Any] = {}

    def fake(conn: Any, symbol: str, *, limit: int) -> dict[str, Any]:
        seen.update(symbol=symbol, limit=limit)
        return {"symbol": symbol.upper(), "filing_days": 0, "prints": [], "n": 0, "median_ratio": None, "rich": 0}

    monkeypatch.setattr(risk_stats, "_connect_or_503", lambda: _C())
    monkeypatch.setattr(risk_stats, "earnings_moves", fake)
    body = TestClient(create_app()).get("/analytics/vol/earnings-moves?symbol=qqqq&limit=4").json()
    assert body["ok"] is True and body["data"]["filing_days"] == 0
    assert seen == {"symbol": "qqqq", "limit": 4}
