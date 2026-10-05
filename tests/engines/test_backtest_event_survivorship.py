"""B6 / B7 on the event backtest: 8-K release dates, delistings (W2, 0.171.0).

Uses the fake Golden Source of ``test_backtest_event_query``.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from bifrost_research.engines.backtest.event_defs import EventDef
from bifrost_research.engines.backtest.event_query import run_event_query
from tests.engines.test_backtest_event_query import _FakeConn, _FakeState, _weekday_bars



# ---------------------------------------------------------------------------
# B6 — earnings dates are the 8-K results release, not the 10-Q filing
# ---------------------------------------------------------------------------


_RESULTS = "Item 2.02 Results of Operations and Financial Condition. Fourth quarter revenue rose."
_DELIVERIES = "Item 2.02 Results of Operations and Financial Condition. Production and deliveries update."


def test_earnings_resolve_to_8k_results_releases() -> None:
    today = date(2026, 6, 1)
    state = _FakeState()
    state.filings_8k = [
        ("NVDA", date(2026, 2, 25), _RESULTS),
        ("NVDA", date(2026, 2, 27), _RESULTS),  # 8-K/A two days on: the same print
        ("TSLA", date(2026, 4, 2), _DELIVERIES),  # set aside: a release follows
        ("TSLA", date(2026, 4, 22), _RESULTS),
    ]
    # A 10-Q filed weeks after the release must not be read at all.
    state.corp_actions.append(("NVDA", "earnings", date(2026, 3, 20)))
    from bifrost_research.engines.backtest import event_query

    got = event_query.resolve_events(
        _FakeConn(state),
        EventDef(kind="earnings", params={"symbols": ["NVDA", "TSLA", "AMD"]}),
        1,
        today=today,
    )
    assert got.source == "sec_8k_item_2_02"
    assert got.events == [("NVDA", date(2026, 2, 25)), ("TSLA", date(2026, 4, 22))]
    assert "offset <= -1" in got.notes
    assert "left out: AMD" in got.notes


def test_earnings_8k_under_an_old_ticker_come_back_under_the_new_one() -> None:
    state = _FakeState()
    state.filings_8k = [("SATS", date(2026, 5, 8), _RESULTS)]
    from bifrost_research.engines.backtest import event_query

    got = event_query.resolve_events(
        _FakeConn(state), EventDef(kind="earnings", params={"symbols": ["ECHO"]}), 1, today=date(2026, 6, 1)
    )
    assert got.events == [("ECHO", date(2026, 5, 8))]


# ---------------------------------------------------------------------------
# B7 — a delisted underlying closes at its last close instead of dropping out
# ---------------------------------------------------------------------------


def test_delisted_underlying_exits_at_its_last_close() -> None:
    today = date(2026, 10, 1)
    event = date(2026, 8, 11)
    last = date(2026, 8, 14)
    state = _FakeState()
    bars = _weekday_bars("AVB", event - timedelta(days=30), last)
    for b in bars:
        if b.bar_date == last:
            b.close = 90.0
    state.stock["AVB"] = bars
    state.inactive.add("AVB")
    state.corp_actions.append(("AVB", "earnings", event))
    result = run_event_query(
        EventDef(kind="earnings", params={"symbols": ["AVB"]}),
        template_name="long_stock_event",
        lookback_years=1,
        conn=_FakeConn(state),
        today=today,
        entry_offset_days=-1,
        exit_offset_days=10,
    )
    s = result["summary"]
    assert s["n_events"] == 1 and s["skipped_incomplete_window"] == 0
    assert s["delisted_exits"] == 1
    run = result["runs"][0]
    assert run["exit_ts"] == last.isoformat()
    assert run["notes"].startswith("delisted")
    assert run["legs"][0]["exit_price"] == pytest.approx(90.0)


def test_a_live_name_past_its_data_is_still_skipped() -> None:
    # Not retired (ticker active): an exit that has not happened is not priced.
    today = date(2026, 10, 1)
    event = date(2026, 8, 11)
    state = _FakeState()
    state.stock["XYZ"] = _weekday_bars("XYZ", event - timedelta(days=30), date(2026, 8, 14))
    state.corp_actions.append(("XYZ", "earnings", event))
    result = run_event_query(
        EventDef(kind="earnings", params={"symbols": ["XYZ"]}),
        template_name="long_stock_event",
        lookback_years=1,
        conn=_FakeConn(state),
        today=today,
        entry_offset_days=-1,
        exit_offset_days=10,
    )
    assert result["summary"]["n_events"] == 0
    assert result["summary"]["delisted_exits"] == 0


def test_an_unreadable_8k_table_is_reported_not_replaced_by_the_stub() -> None:
    # A role without SELECT on raw_market.sec_8k_filing (2026-10-05) fell
    # through to the stub calendar and said only "stub".
    class _NoGrant(_FakeConn):
        rollbacks = 0

        def cursor(self):  # type: ignore[override]
            cur = super().cursor()
            execute = cur.execute

            def guarded(query: str, params: object = None) -> None:
                if "raw_market.sec_8k_filing" in query:
                    raise RuntimeError("permission denied for table sec_8k_filing")
                execute(query, params)

            cur.execute = guarded  # type: ignore[method-assign]
            return cur

        def rollback(self) -> None:
            type(self).rollbacks += 1

    conn = _NoGrant(_FakeState())
    result = run_event_query(
        EventDef(kind="earnings", params={"symbols": ["NVDA"]}),
        template_name="long_stock_event",
        lookback_years=1,
        conn=conn,
        today=date(2026, 6, 1),
    )
    assert result["event_source"] == "unavailable"
    assert result["summary"]["n_events"] == 0 and result["runs"] == []
    (err,) = result["event_source_errors"]
    assert err.startswith("raw_market.sec_8k_filing: RuntimeError: permission denied")
    assert _NoGrant.rollbacks == 1
