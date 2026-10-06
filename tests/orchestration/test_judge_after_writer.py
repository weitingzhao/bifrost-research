"""Ratchet (TD-97, TD-156): a job that reads what research_trading_day writes runs after it.

alert_scan ran at 22:30 UTC, four hours before the batch wrote the session's scan
(22:30 New York = 02:30 UTC), so it judged every session a day late from the
previous night's rows; signal_hit did the same at 00:10 UTC with every lens
source. An entry in ``READS_TRADING_DAY_OUTPUT`` must either sit in
research_trading_day downstream of every writer it names, or be scheduled
outside the window.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("dagster")

from dagster import AssetKey

from bifrost_research.orchestration.research_aux_schedules import READS_TRADING_DAY_OUTPUT

#: Between the close and the end of the batch the session's features are missing.
WINDOW = (time(20, 0), time(2, 30))
#: One week in daylight time and one in standard time: the batch moves an hour.
WEEKS = (datetime(2026, 7, 6, tzinfo=timezone.utc), datetime(2026, 1, 5, tzinfo=timezone.utc))


def _in_window(t: time) -> bool:
    return t >= WINDOW[0] or t < WINDOW[1]


def _fires_utc(cron: str, tz: str) -> list[datetime]:
    croniter = pytest.importorskip("croniter").croniter
    out: list[datetime] = []
    for start in WEEKS:
        local = start.astimezone(ZoneInfo(tz))
        it = croniter(cron, local)
        while True:
            nxt = it.get_next(datetime)
            if nxt >= local + timedelta(days=7):
                break
            out.append(nxt.astimezone(timezone.utc))
    return out


def _ancestors(graph, key: AssetKey) -> set[AssetKey]:
    seen: set[AssetKey] = set()
    todo = [key]
    while todo:
        for parent in graph.get(todo.pop()).parent_keys:
            if parent not in seen:
                seen.add(parent)
                todo.append(parent)
    return seen


def _defs():
    from bifrost_research.orchestration.definitions import defs

    return defs


def test_the_map_names_real_assets() -> None:
    graph = _defs().resolve_asset_graph()
    keys = {k.to_user_string() for k in graph.get_all_asset_keys()}
    for reader, sources in READS_TRADING_DAY_OUTPUT.items():
        assert reader in keys and sources, reader
        for _table, writer in sources:
            assert writer in keys, (reader, writer)


def test_readers_in_the_batch_wait_for_their_writer() -> None:
    defs = _defs()
    graph = defs.resolve_asset_graph()
    batch = defs.resolve_job_def("research_trading_day").asset_layer.executable_asset_keys
    for reader, sources in READS_TRADING_DAY_OUTPUT.items():
        rkey = AssetKey(reader.split("/"))
        if rkey not in batch:
            continue
        ancestors = _ancestors(graph, rkey)
        for table, writer in sources:
            wkey = AssetKey(writer.split("/"))
            assert wkey in batch, f"{reader} is in research_trading_day but its writer {writer} is not"
            assert wkey in ancestors, f"{reader} reads {table} but does not wait for {writer}"


def test_signal_hit_is_registered_for_every_lens_source() -> None:
    """TD-156: each decay lens's source table names its writer (or is SVI's)."""
    from bifrost_research.engines.signal_hit.entry import LENS_SOURCE

    named = {table for table, _writer in READS_TRADING_DAY_OUTPUT["engines/signal_hit"]}
    # Skew reads SVI's fit, written at 23:20 UTC by research_vol_surface_svi_schedule
    # before the batch starts; it is not a trading-day table.
    outside_the_batch = {"features.option_surface_fit_daily"}
    assert {t for t, _ in LENS_SOURCE.values()} - outside_the_batch <= named


def test_signal_hit_runs_in_the_batch() -> None:
    batch = _defs().resolve_job_def("research_trading_day").asset_layer.executable_asset_keys
    assert AssetKey(["engines", "signal_hit"]) in batch


def test_no_schedule_fires_a_reader_before_the_batch_has_written() -> None:
    defs = _defs()
    offenders: list[str] = []
    for schedule in defs.schedules or []:
        if schedule.job_name == "research_trading_day":
            continue
        job = defs.resolve_job_def(schedule.job_name)
        selected = {k.to_user_string() for k in job.asset_layer.executable_asset_keys}
        readers = selected & set(READS_TRADING_DAY_OUTPUT)
        if not readers:
            continue
        early = [
            t.isoformat()
            for t in _fires_utc(schedule.cron_schedule, schedule.execution_timezone or "UTC")
            if _in_window(t.time())
        ]
        if early:
            offenders.append(f"{schedule.name} ({sorted(readers)}): {early[:2]}")
    assert not offenders, f"fires inside 20:00-02:30 UTC: {offenders}"


def test_the_old_alert_scan_schedule_would_have_been_caught() -> None:
    assert all(_in_window(t.time()) for t in _fires_utc("30 22 * * 1-5", "UTC"))
    # signal_hit's own schedule until TD-156.
    assert all(_in_window(t.time()) for t in _fires_utc("10 0 * * 1-6", "UTC"))
    # The batch itself: 22:30 New York is 02:30 UTC (EDT) / 03:30 UTC (EST).
    assert not any(_in_window(t.time()) for t in _fires_utc("30 22 * * 1-5", "America/New_York"))
