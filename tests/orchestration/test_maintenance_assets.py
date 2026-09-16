"""The hand-run maintenance assets: purge and deep signal backfill (R9 C1 / C2)."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("dagster")

from dagster import build_asset_context

from bifrost_research.orchestration import research_aux_schedules as aux


def test_the_purge_asset_passes_force_through_and_defaults_to_a_dry_run(monkeypatch) -> None:
    seen: list[bool] = []

    def _fake(*, force: bool = False) -> dict[str, Any]:
        seen.append(force)
        return {"engine": "event_radar_purge", "mode": "dry_run", "matched": 17, "deleted": 0}

    monkeypatch.setattr(aux.runners, "run_event_radar_purge", _fake)
    aux.maint_event_radar_purge(build_asset_context(), aux.EventRadarPurgeConfig())
    aux.maint_event_radar_purge(build_asset_context(), aux.EventRadarPurgeConfig(force=True))
    assert seen == [False, True]


def test_the_signal_backfill_asset_runs_the_slot_with_its_lookback(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        aux.engine_sched,
        "run_slot",
        lambda slot, *, lookback_days: calls.append((slot, lookback_days)) or {"slot": slot},
    )
    aux.maint_signal_backfill(
        build_asset_context(), aux.SignalBackfillConfig(slot="gex", lookback_days=60)
    )
    assert calls == [("gex", 60)]


def test_the_signal_backfill_asset_refuses_a_slot_it_was_not_meant_to_run(monkeypatch) -> None:
    monkeypatch.setattr(
        aux.engine_sched,
        "run_slot",
        lambda *a, **k: pytest.fail("must not run an unlisted slot"),
    )
    with pytest.raises(ValueError, match="slot must be one of"):
        aux.maint_signal_backfill(
            build_asset_context(), aux.SignalBackfillConfig(slot="forecast", lookback_days=60)
        )


def test_neither_asset_is_in_the_trading_day_job() -> None:
    from bifrost_research.orchestration.definitions import defs

    graph = defs.resolve_asset_graph()
    job = next(j for j in defs.jobs if j.name == "research_trading_day")
    daily = {"/".join(k.path) for k in job.selection.resolve(graph)}
    assert "maintenance/event_radar_purge" not in daily
    assert "maintenance/signal_backfill" not in daily
    assert "maintenance/terrain_backfill" not in daily
