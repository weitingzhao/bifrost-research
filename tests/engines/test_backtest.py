"""Wave 4.4 Settlement / backtest accuracy tests."""

from __future__ import annotations

from datetime import date

from bifrost_research.engines.backtest.settlement import (
    aggregate_accuracy,
    settle_forecast,
)


def test_settle_close_miss_and_path() -> None:
    hourly = [
        {
            "hour_et": 10,
            "path_call": "mean-revert",
            "level_low": 99.0,
            "level_high": 101.0,
            "level_target": 100.0,
        },
        {
            "hour_et": 15,
            "path_call": "mean-revert->close",
            "level_low": 99.0,
            "level_high": 101.0,
            "level_target": 100.2,
        },
    ]
    stl = settle_forecast(
        session_id="s1",
        symbol="SPY",
        trade_date=date(2024, 6, 3),
        expected_close=100.0,
        hourly=hourly,
        actual_close=100.5,
        hourly_actuals={10: 100.1, 15: 100.4},
    )
    assert stl.close_miss == 0.5
    assert abs(stl.close_miss_pct - 0.005) < 1e-9
    assert stl.path_total == 2
    assert stl.path_hit_count >= 1
    assert "D10" in stl.notes


def test_aggregate_accuracy() -> None:
    a = settle_forecast(
        session_id="a",
        symbol="QQQ",
        trade_date=date(2024, 6, 3),
        expected_close=400.0,
        hourly=[
            {
                "hour_et": 12,
                "path_call": "coil",
                "level_low": 398,
                "level_high": 402,
                "level_target": 400,
            }
        ],
        actual_close=400.2,
        hourly_actuals={12: 400.1},
    )
    b = settle_forecast(
        session_id="b",
        symbol="QQQ",
        trade_date=date(2024, 6, 4),
        expected_close=401.0,
        hourly=[
            {
                "hour_et": 12,
                "path_call": "higher-high",
                "level_low": 400,
                "level_high": 410,
                "level_target": 405,
            }
        ],
        actual_close=390.0,
        hourly_actuals={12: 392.0},
    )
    summary = aggregate_accuracy([a, b], symbol="QQQ")
    assert summary.sessions_settled == 2
    assert 0.0 <= summary.path_hit_rate <= 1.0
    assert summary.avg_close_miss_pct >= 0
    assert summary.period_start <= summary.period_end


# ─── 2026-09-26: settle against the session that follows, judge only what printed ───

from datetime import datetime, timezone  # noqa: E402

from bifrost_research.engines.backtest.settlement import load_hourly_closes  # noqa: E402

_PATH = [
    {"hour_et": 10, "path_call": "higher-high", "level_low": 101.0, "level_high": 103.0, "level_target": 102.0},
    {"hour_et": 15, "path_call": "higher-high->close", "level_low": 101.0, "level_high": 103.0, "level_target": 102.0},
]


def test_hours_without_prints_are_not_judged_against_the_close() -> None:
    stl = settle_forecast(
        session_id="PLTR-2026-09-23",
        symbol="PLTR",
        trade_date=date(2026, 9, 23),
        expected_close=102.0,
        hourly=_PATH,
        actual_close=102.5,
        spot=100.0,
        target_date=date(2026, 9, 24),
    )
    assert all(h.actual_price is None for h in stl.hourly)
    assert stl.path_total == 0
    assert stl.path_hit_count == 0
    assert stl.stats_json["path_basis"] == "close"
    assert stl.stats_json["hours_judged"] == 0
    assert stl.stats_json["forecast_hours"] == 2
    # the close alone: 102.5 is within 1% of 102.0
    assert stl.path_hit is True
    assert stl.stats_json["target_date"] == "2026-09-24"


def test_path_is_judged_on_the_hours_that_printed() -> None:
    stl = settle_forecast(
        session_id="PLTR-2026-09-23",
        symbol="PLTR",
        trade_date=date(2026, 9, 23),
        expected_close=102.0,
        hourly=_PATH,
        actual_close=102.1,
        hourly_actuals={10: 102.2},
        spot=100.0,
    )
    assert stl.path_total == 1
    assert stl.stats_json["path_basis"] == "hourly"
    assert stl.hourly[1].actual_price is None


def test_direction_is_the_called_move_against_the_move_that_came() -> None:
    kw = dict(session_id="s", symbol="PLTR", trade_date=date(2026, 9, 23), hourly=[], spot=100.0)
    # called a fall to 98, price rose to 101 from 100 → wrong way, though the old
    # «close at or above the target» rule scored 101 >= 98 as a hit
    up = settle_forecast(expected_close=98.0, actual_close=101.0, **kw)  # type: ignore[arg-type]
    assert up.stats_json["direction_hit"] is False
    down = settle_forecast(expected_close=98.0, actual_close=97.0, **kw)  # type: ignore[arg-type]
    assert down.stats_json["direction_hit"] is True
    # a call within a tenth of a percent is «flat» and holds if the day stays within half a percent
    flat = settle_forecast(expected_close=100.05, actual_close=100.3, **kw)  # type: ignore[arg-type]
    assert flat.stats_json["direction_hit"] is True


def test_settlement_id_is_one_per_session() -> None:
    a = settle_forecast(
        session_id="PLTR-2026-09-23", symbol="PLTR", trade_date=date(2026, 9, 23),
        expected_close=102.0, hourly=[], actual_close=101.0,
    )
    b = settle_forecast(
        session_id="PLTR-2026-09-23", symbol="PLTR", trade_date=date(2026, 9, 23),
        expected_close=102.0, hourly=[], actual_close=101.0, hourly_actuals={10: 101.0},
    )
    assert a.settlement_id == b.settlement_id == "stl-PLTR-2026-09-23"


class _RowsCursor:
    def __init__(self, rows):
        self.rows = rows
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def execute(self, sql, params=None):
        self.params = params

    def fetchall(self):
        return self.rows


class _RowsConn:
    def __init__(self, rows):
        self.cur = _RowsCursor(rows)

    def cursor(self):
        return self.cur


def test_hourly_closes_key_each_bar_by_the_hour_it_ends() -> None:
    # Bars are labelled by their start: 13:00 UTC on 2026-09-24 is 09:00 ET (EDT),
    # so its close is the price at 10:00 ET. Pre- and post-market bars fall out.
    rows = [
        (datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc), 188.5),   # 08:00 ET → 09:00, dropped
        (datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc), 190.245),  # 09:00 ET → 10:00
        (datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc), 192.62),   # 15:00 ET → 16:00
        (datetime(2026, 9, 24, 21, 0, tzinfo=timezone.utc), 192.0),    # 17:00 ET → dropped
    ]
    conn = _RowsConn(rows)
    out = load_hourly_closes(conn, "pltr", date(2026, 9, 24))
    assert out == {10: 190.245, 16: 192.62}
    sym, start, end = conn.cur.params
    assert sym == "PLTR"
    assert start.utcoffset() is not None and (end - start).days == 1


# ─── 2026-09-26: a settlement drawn from a faulty input scores that input ───

from bifrost_research.engines.backtest.settlement import (
    forecast_result_sql,
    input_fault_count_sql,
)


def test_input_fault_is_stamped_and_left_out_of_the_aggregate() -> None:
    faulty = settle_forecast(
        session_id="PLTR-2026-07-27",
        symbol="PLTR",
        trade_date=date(2026, 7, 27),
        expected_close=20.502,
        hourly=[],
        actual_close=123.53,
        spot=131.53,
        target_date=date(2026, 7, 28),
        input_fault="walls_off_spot",
    )
    assert faulty.stats_json["input_fault"] == "walls_off_spot"
    fine = settle_forecast(
        session_id="PLTR-2026-09-23",
        symbol="PLTR",
        trade_date=date(2026, 9, 23),
        expected_close=190.0,
        hourly=[],
        actual_close=191.0,
        spot=191.79,
        target_date=date(2026, 9, 24),
    )
    assert "input_fault" not in fine.stats_json
    summary = aggregate_accuracy([faulty, fine], symbol="PLTR")
    assert summary.sessions_settled == 1
    assert summary.avg_close_miss_pct < 0.01
    assert summary.stats_json["input_faults"] == 1
    only_faults = aggregate_accuracy([faulty], symbol="PLTR")
    assert only_faults.sessions_settled == 0
    assert only_faults.stats_json["input_faults"] == 1


def test_forecast_result_sql_treats_a_missing_stats_json_as_a_result() -> None:
    # A NULL stats_json must not drop the row: `NULL ? key` is NULL, and NOT NULL
    # filters it out.
    assert forecast_result_sql() == "NOT COALESCE(stats_json ? 'input_fault', false)"
    assert forecast_result_sql("st") == "NOT COALESCE(st.stats_json ? 'input_fault', false)"
    assert input_fault_count_sql("s").startswith("COUNT(*) FILTER (WHERE NOT (NOT COALESCE(s.stats_json")
