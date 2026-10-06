"""W6 — Pine script library, runner client and daily build (runner mocked)."""

from __future__ import annotations

from datetime import date

import pytest

from bifrost_research.engines.backtest import event_query
from bifrost_research.engines.backtest.event_defs import EventDef
from bifrost_research.engines.pine import client
from bifrost_research.engines.pine.build import signal_rows
from bifrost_research.engines.pine.library import PineScript, builtin_scripts, signal_sides, validate


def test_every_builtin_plots_buy_and_sell_and_is_ours() -> None:
    scripts = builtin_scripts()
    assert len(scripts) == 8
    for s in scripts:
        assert signal_sides(s.source) == ["buy", "sell"], s.id
        assert s.origin == "bifrost" and "Bifrost" in (s.license or "")
        assert "request." not in s.source
        validate(s.id, s.name, s.source)
    assert {s.id for s in scripts} >= {"supertrend", "squeeze_momentum", "wavetrend"}


def test_validate_refuses_a_script_without_signal_plots_or_a_bad_id() -> None:
    with pytest.raises(ValueError, match="buy"):
        validate("ok_id", "x", '//@version=5\nindicator("x")\nplot(close, "price")')
    with pytest.raises(ValueError, match="id"):
        validate("Bad-Id", "x", 'plotshape(true, "buy")')


def test_client_sends_ms_dates_and_reads_back_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict = {}

    def fake_post(path, payload, timeout):  # noqa: ANN001
        sent.update(payload)
        t = payload["series"][0]["bars"][1]["t"]
        return {"ok": True, "results": [{"symbol": "AAA", "buy": [t], "sell": []}, {"symbol": "BBB", "error": "boom"}]}

    monkeypatch.setattr(client, "_post", fake_post)
    bars = [{"date": date(2024, 1, 2), "close": 10.0}, {"date": date(2024, 1, 3), "close": 11.0, "open": 10.5}]
    out = client.run("src", {"AAA": bars, "BBB": bars})
    assert sent["series"][0]["bars"][0]["o"] == 10.0  # a missing open falls back to the close
    assert out["AAA"]["buy"] == [date(2024, 1, 3)] and out["BBB"] == {"error": "boom"}



def test_client_carries_runner_warnings_and_tolerates_an_older_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    warn = {"code": "duplicate_title", "side": "buy", "count": 2, "message": "2 plots are titled \"buy\""}

    def fake_post(path, payload, timeout):  # noqa: ANN001
        return {
            "ok": True,
            "results": [
                {"symbol": "AAA", "buy": [], "sell": [], "warnings": [warn]},
                {"symbol": "BBB", "buy": [], "sell": []},  # runner 0.1.0 sends no warnings
            ],
        }

    monkeypatch.setattr(client, "_post", fake_post)
    bars = [{"date": date(2024, 1, 2), "close": 10.0}]
    out = client.run("src", {"AAA": bars, "BBB": bars})
    assert out["AAA"]["warnings"] == [warn]
    assert out["BBB"]["warnings"] == []

def test_signal_rows_keep_only_recent_sessions_and_report_errors() -> None:
    s = PineScript(id="x", name="x", source='plotshape(true, "buy")', version=3)
    bars = {"AAA": [{"date": date(2024, 1, 2), "close": 10.0}, {"date": date(2024, 1, 9), "close": 12.0}]}
    fired = {"AAA": {"buy": [date(2024, 1, 2), date(2024, 1, 9)], "sell": []}, "BBB": {"error": "bad"}}
    rows, errors = signal_rows(s, bars, fired, since=date(2024, 1, 5))
    assert rows == [("x", "AAA", date(2024, 1, 9), "buy", 3, 12.0)]
    assert errors == {"BBB": "bad"}


class _Cur:
    def __init__(self, rows):  # noqa: ANN001
        self.rows, self.sql, self.args = rows, None, None

    def __enter__(self):
        return self

    def __exit__(self, *a):  # noqa: ANN002
        return False

    def execute(self, sql, args=None):  # noqa: ANN001
        self.sql, self.args = sql, args

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):  # noqa: ANN001
        self.cur = _Cur(rows)

    def cursor(self):
        return self.cur

    def rollback(self):
        pass


def test_pine_signal_event_kind_reads_the_signal_table() -> None:
    conn = _Conn([("NVDA", date(2024, 3, 1))])
    ed = EventDef.from_dict({"kind": "pine_signal", "params": {"script": "supertrend", "side": "sell", "symbols": ["nvda"]}})
    res = event_query.resolve_events_between(conn, ed, date(2024, 1, 1), date(2024, 6, 1))
    assert res.events == [("NVDA", date(2024, 3, 1))] and res.source == "pine"
    assert "stock_signal_pine_daily" in conn.cur.sql
    assert conn.cur.args[:2] == ("supertrend", "sell") and conn.cur.args[-1] == ["NVDA"]


def test_pine_signal_needs_a_script_and_a_side() -> None:
    with pytest.raises(ValueError):
        event_query.resolve_events_between(_Conn([]), EventDef.from_dict({"kind": "pine_signal", "params": {}}), date(2024, 1, 1), date(2024, 2, 1))
    with pytest.raises(ValueError):
        event_query.resolve_events_between(
            _Conn([]), EventDef.from_dict({"kind": "pine_signal", "params": {"script": "x", "side": "up"}}), date(2024, 1, 1), date(2024, 2, 1)
        )


def test_stats_sql_is_built_for_each_horizon() -> None:
    from bifrost_research.api.pine import _stats_sql

    sql = _stats_sql([5, 20], with_symbols=True)
    assert "LEAD(close, 5)" in sql and "LEAD(close, 20)" in sql and "%(symbols)s" in sql
    assert "sw20" in sql and "bh5" in sql
