"""Readers that set a stock price against strikes read the close as printed (0.156.0).

The vendor's adjusted close folds in later splits and spin-offs, and reports no
spin-offs to undo it with: HON 2025-10-29 is 200.65 adjusted, 212.89 as traded,
and that day's chain puts it at 212.10 by put-call parity. Plugin 0.74.0 stores
the printed close as ``raw_market.stock_daily.close_unadjusted``; these readers
take it first and fall back to ``close`` where the plugin has not filled it.
Returns-based readers (realised vol, momentum, outcomes) keep the adjusted close,
which is the continuous series.
"""

from __future__ import annotations

import inspect
from datetime import date
from typing import Any

from bifrost_research.engines.backtest import event_query
from bifrost_research.engines.gex import exposure

AS_PRINTED = "COALESCE(close_unadjusted, close)"


class _Conn:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.statements: list[str] = []
        self._rows: list[Any] = []

    def cursor(self) -> _Conn:
        return self

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.statements.append(sql)
        self._rows = list(self.answer(sql))

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows


def test_gex_spot_is_the_printed_close() -> None:
    conn = _Conn(lambda sql: [(212.89,)] if "raw_market.stock_daily" in sql else [])
    reading = exposure.fetch_spot_reading(conn, "HON", date(2025, 10, 29))
    assert reading == (212.89, "close", date(2025, 10, 29))
    assert AS_PRINTED in conn.statements[0]


def test_gex_prior_close_is_the_printed_close() -> None:
    def answer(sql: str) -> list[Any]:
        return [(212.89, date(2025, 10, 29))] if "bar_date < %s" in sql else []

    conn = _Conn(answer)
    reading = exposure.fetch_spot_reading(conn, "HON", date(2025, 10, 30), prior_close_days=7)
    assert reading == (212.89, "prior_close", date(2025, 10, 29))
    assert any(AS_PRINTED in s and "bar_date < %s" in s for s in conn.statements)


def test_an_option_leg_strikes_off_the_printed_close_and_a_stock_leg_off_the_adjusted() -> None:
    row = (date(2025, 10, 29), 201.0, 200.6503, 212.89)
    conn = _Conn(lambda sql: [row] if "raw_market.stock_daily" in sql else [])
    price = event_query._fetch_stock_price(conn, "HON", date(2025, 10, 29))
    assert price == {"bar_date": date(2025, 10, 29), "open": 201.0, "close": 200.6503, "close_as_traded": 212.89}

    seen: dict[str, float] = {}

    def pick(conn: Any, **kw: Any) -> None:
        seen["strike_target"] = kw["strike_target"]

    original = event_query._pick_option
    event_query._pick_option = pick  # type: ignore[assignment]
    try:
        leg = event_query.LegSpec(kind="option", side="buy", option_right="C", target_dte=30, target_moneyness_offset=0.0)
        event_query._price_option_leg(conn, "HON", date(2025, 10, 29), date(2025, 11, 28), leg)
    finally:
        event_query._pick_option = original  # type: ignore[assignment]
    assert seen["strike_target"] == 212.89


def test_the_pin_readers_compare_strikes_with_the_printed_close() -> None:
    from bifrost_research.api import similar_regime
    from bifrost_research.engines.forecast import terrain
    from bifrost_research.repositories import opex_cycle

    assert AS_PRINTED + " AS close" in inspect.getsource(opex_cycle.get_pin_analysis)
    assert "COALESCE(s.close_unadjusted, s.close)::float - n.max_pain_strike" in inspect.getsource(similar_regime)
    assert AS_PRINTED + " AS close FROM raw_market.stock_daily" in inspect.getsource(terrain)
