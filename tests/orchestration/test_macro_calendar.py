"""Ratchet (TD-151): the macro calendar has a caller, a source, and a horizon check.

features.macro_event_daily sat at 0 rows until 2026-10-06: ``macro_ingest`` was a
CSV drop-zone reader with no schedule, so the Events board's "Macro forward" panel
was always empty. The packaged calendar is now ingested every Monday and the
asset's output check turns ERROR when the calendar reaches less than 30 days ahead.
"""

from __future__ import annotations

import tomllib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bifrost_research.api.schedule_roster import ROSTER_BY_NAME
from bifrost_research.scheduler import macro_ingest as mi

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)


def _seed() -> list[tuple[Any, ...]]:
    return mi.parse_macro_csv(mi.seed_text(), source=mi.SEED_SOURCE, now=NOW, default_forward=True)


def _col(name: str) -> int:
    return mi._MACRO_COLS.index(name)


def test_seed_parses_into_forward_rows_with_stable_ids() -> None:
    rows = _seed()
    assert len(rows) >= 19
    ids = [r[0] for r in rows]
    assert len(ids) == len(set(ids))
    assert ids == [r[0] for r in _seed()], "a re-ingest must hit the same keys"
    assert all(r[_col("forward_flag")] is True for r in rows)
    assert all(r[_col("source")] == mi.SEED_SOURCE for r in rows)
    assert all(r[_col("release_ts")] is not None for r in rows)


def test_fomc_decisions_are_wednesdays_at_two_pm_new_york() -> None:
    fomc = [r for r in _seed() if r[_col("indicator")] == "FOMC rate decision"]
    assert len(fomc) == 16
    for row in fomc:
        assert row[_col("event_date")].weekday() == 2, row[0]
        ts = row[_col("release_ts")].astimezone(mi._NY)
        assert (ts.hour, ts.minute) == (14, 0)


#: Every series the seed carries; each must keep a date ahead of the injected day.
SERIES = ("FOMC rate decision", "CPI", "NFP")


def _series_without_a_future_date(rows: list[tuple[Any, ...]], today: date) -> list[str]:
    ahead = {r[_col("indicator")] for r in rows if r[_col("event_date")] > today}
    return [s for s in SERIES if s not in ahead]


def test_every_series_has_a_future_date() -> None:
    """Ratchet (TD-180): each series still has a release ahead of the injected day.

    Not "last date >= today + 180": BLS schedules about 14 months out, so that
    line goes red in an ordinary year. The day is injected, never the clock.
    """
    assert _series_without_a_future_date(_seed(), date(2026, 10, 7)) == []


def test_series_check_names_a_series_that_ran_out() -> None:
    rows = _seed()
    assert _series_without_a_future_date(rows, date(2026, 12, 4)) == ["NFP"]
    assert _series_without_a_future_date(rows, date(2026, 12, 10)) == ["CPI", "NFP"]


def test_bls_releases_are_at_eight_thirty_new_york() -> None:
    bls = [r for r in _seed() if r[_col("indicator")] in ("CPI", "NFP")]
    assert {r[_col("indicator")] for r in bls} == {"CPI", "NFP"}
    for row in bls:
        ts = row[_col("release_ts")].astimezone(mi._NY)
        assert (ts.hour, ts.minute) == (8, 30), row[0]


def test_seed_ships_in_the_wheel() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["package-data"]["bifrost_research"]
    assert "scheduler/data/*.csv" in patterns


def test_drop_file_without_forward_column_is_history() -> None:
    text = "event_date,country,indicator,actual,expected,prior\n2026-09-11,US,CPI,2.9,3.0,2.7\n"
    (row,) = mi.parse_macro_csv(text, source=mi.DROP_SOURCE, now=NOW)
    assert row[_col("forward_flag")] is False
    assert row[_col("gap_pct")] == pytest.approx(-0.033333, abs=1e-6)
    assert row[0] == mi.macro_id_for(date(2026, 9, 11), "US", "CPI")


class _Cur:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self.log = log
        self.rowcount = 2

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.log.append((" ".join(sql.split()), params))

    def executemany(self, sql: str, rows: list[Any]) -> None:
        self.log.append((" ".join(sql.split()), len(rows)))

    def fetchall(self) -> list[tuple[str, date]]:
        return [("CPI", date(2026, 12, 10)), ("FOMC rate decision", date(2027, 12, 8))]


class _Conn:
    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []
        self.commits = 0
        self.closed = False

    def cursor(self) -> _Cur:
        return _Cur(self.log)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_run_prunes_removed_seed_rows_then_upserts_and_reads_the_horizon(tmp_path: Path) -> None:
    conn = _Conn()
    out = mi.run_macro_ingest(connect_fn=lambda: conn, now=NOW, input_dir=tmp_path / "absent")
    delete, upsert, horizon = (entry[0] for entry in conn.log)
    assert delete.startswith(f"DELETE FROM {mi.TABLE} WHERE source = %s")
    assert conn.log[0][1][0] == mi.SEED_SOURCE
    assert upsert.startswith(f"INSERT INTO {mi.TABLE}") and "ON CONFLICT (macro_id)" in upsert
    assert "max(event_date)" in horizon
    assert conn.commits == 1 and conn.closed
    assert out["rows_written"] == out["seed_rows"] == len(_seed())
    assert out["pruned_seed_rows"] == 2
    assert out["horizon"] == "2027-12-08"
    assert mi.horizon_findings(out) == []


def test_horizon_check_errors_when_the_calendar_runs_out() -> None:
    result = {"today": "2027-11-20", "horizon": "2027-12-08", "horizon_by_indicator": {"FOMC rate decision": "2027-12-08"}}
    levels = [level for level, _ in mi.horizon_findings(result)]
    assert levels == ["error", "warn"]
    assert [lvl for lvl, _ in mi.horizon_findings({"today": "2026-10-06", "horizon": None})] == ["error"]


def test_horizon_check_warns_when_one_series_runs_out() -> None:
    result = {
        "today": "2026-11-20",
        "horizon": "2027-12-08",
        "horizon_by_indicator": {"CPI": "2026-12-10", "FOMC rate decision": "2027-12-08", "GDP": "2025-01-30"},
    }
    findings = mi.horizon_findings(result)
    assert [level for level, _ in findings] == ["warn"]
    assert "CPI ends 2026-12-10" in findings[0][1]


def test_orchestration_calls_macro_ingest_on_a_weekly_schedule() -> None:
    """The TD-151 acceptance greps orchestration for macro_ingest."""
    src = (ROOT / "src/bifrost_research/orchestration/runners.py").read_text(encoding="utf-8")
    assert "from bifrost_research.scheduler.macro_ingest import run_macro_ingest" in src
    spec = ROSTER_BY_NAME["research_macro_calendar_schedule"]
    assert (spec.job, spec.tz, spec.cron) == ("research_macro_calendar_job", "UTC", "0 10 * * 1")


def test_dagster_asset_carries_the_horizon_check() -> None:
    pytest.importorskip("dagster")
    from bifrost_research.orchestration import asset_checks as ac
    from bifrost_research.orchestration.definitions import defs
    from dagster import AssetCheckKey, AssetKey

    key = AssetKey(["engines", "macro_calendar"])
    graph = defs.resolve_asset_graph()
    assert AssetCheckKey(key, ac.OUTPUT_CHECK) in graph.asset_check_keys
    job = defs.resolve_job_def("research_macro_calendar_job")
    assert key in job.asset_layer.selected_asset_keys

    from bifrost_research.orchestration.research_aux_schedules import _macro_horizon

    findings = _macro_horizon({"today": "2027-11-20", "horizon": "2027-12-08", "horizon_by_indicator": {}})
    assert [s for s, _ in findings] == [ac.ERROR]
