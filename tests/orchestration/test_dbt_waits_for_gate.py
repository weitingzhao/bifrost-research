"""dbt waits for the husbandry gate (Owner decision 2026-09-29).

research_trading_day run e1dd41e5 (2026-09-29 02:30 UTC): ``bifrost_research_dbt_assets``
logged STEP_START at 02:30:21 and ``batch__husbandry_gate`` at 02:30:37 — dbt ran
beside the gate instead of after it. The default dagster-dbt translator gave the dbt
assets only their dbt sources as upstreams, so a failing gate blocked the engines
but not dbt. These tests run the real dbt assets in-process with a stub dbt CLI.

Needs the parsed manifest (``make dbt-parse``); skipped without it, like the
Definitions smoke test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytest.importorskip("dagster")
pytest.importorskip("dagster_dbt")

from dagster import DagsterEventType, materialize
from dagster_dbt import DbtCliResource

from bifrost_research.orchestration import plugin_batch_assets as pba
from bifrost_research.orchestration.paths import (
    DBT_MANIFEST_PATH,
    DBT_PROFILES_DIR,
    DBT_PROJECT_DIR,
)

pytestmark = pytest.mark.skipif(
    not DBT_MANIFEST_PATH.is_file(),
    reason="dbt target/manifest.json not present — run `make dbt-parse` first",
)

GATE = pba.husbandry_gate.key
GATE_STEP = "batch__husbandry_gate"
DBT_STEP = "bifrost_research_dbt_assets"

# dbt invocations seen by the stub; the resource is a frozen pydantic model.
_DBT_CALLS: list[list[str]] = []


class _NoEvents:
    """dbt's outputs are all optional, so an empty stream is a successful step."""

    def stream(self) -> Any:
        return iter(())


class StubDbtCli(DbtCliResource):
    """Records the invocation instead of shelling out to dbt."""

    def cli(self, args: Any, **_kw: Any) -> Any:  # type: ignore[override]
        _DBT_CALLS.append(list(args))
        return _NoEvents()


def _stub_gate(monkeypatch: pytest.MonkeyPatch, *, eod_verdict: str) -> None:
    recent = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def fake_get(url: str, **_kw: Any) -> dict[str, Any]:
        if "/market/doctor" in url:
            return {
                "session": "2026-09-28",
                "generated_at": "2026-09-29T02:30:37+00:00",
                "verdict": "degraded",
                "eod_critical": {"verdict": eod_verdict, "detail": "chain coverage 41%"},
            }
        if url.endswith("/flex/config/summary"):
            return {"source": "secret"}
        return {"dimensions": [{"kind": "flex-trades", "last_ok": True, "last_success_at": recent}]}

    monkeypatch.setattr(pba, "get_json", fake_get)


def _dbt_assets() -> list[Any]:
    from bifrost_research.orchestration.dbt_assets import load_dbt_assets

    assets = load_dbt_assets()
    assert assets, "manifest present but no dbt assets loaded"
    return assets


def _run(assets: list[Any]) -> tuple[Any, list[tuple[DagsterEventType, str | None]]]:
    _DBT_CALLS.clear()
    result = materialize(
        [pba.husbandry_gate, *assets],
        resources={
            "dbt": StubDbtCli(project_dir=str(DBT_PROJECT_DIR), profiles_dir=str(DBT_PROFILES_DIR))
        },
        raise_on_error=False,
    )
    events = [(e.event_type, e.step_key) for e in result.all_events]
    return result, events


def _index(
    events: list[tuple[DagsterEventType, str | None]], kind: DagsterEventType, step: str
) -> int:
    return events.index((kind, step))


def test_every_dbt_asset_has_the_gate_as_parent() -> None:
    from bifrost_research.orchestration.definitions import defs

    graph = defs.resolve_asset_graph()
    dbt_keys = {k for a in _dbt_assets() for k in a.keys}
    ungated = sorted(k.to_user_string() for k in dbt_keys if GATE not in graph.get(k).parent_keys)
    assert not ungated, f"{len(ungated)} dbt assets do not wait for the gate: {ungated[:5]}"
    # The source-derived deps are kept alongside the gate.
    parents = set().union(*(graph.get(k).parent_keys for k in dbt_keys)) - dbt_keys
    assert any(p.path[0] == "market" for p in parents), sorted(parents)


def test_research_trading_day_orders_dbt_after_the_gate() -> None:
    """The job that ran e1dd41e5 selects both, so the edge is in its plan."""
    from bifrost_research.orchestration.definitions import defs

    job = defs.resolve_job_def("research_trading_day")
    selected = job.asset_layer.executable_asset_keys
    dbt_keys = {k for a in _dbt_assets() for k in a.keys}
    assert GATE in selected
    assert dbt_keys <= selected

    upstream = {
        dep.node
        for node, inputs in job.dependencies.items()
        if node.name == DBT_STEP
        for dep in inputs.values()
    }
    assert GATE_STEP in upstream


def test_failing_gate_means_dbt_never_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_gate(monkeypatch, eod_verdict="critical")
    result, events = _run(_dbt_assets())

    assert not result.success
    assert (DagsterEventType.STEP_FAILURE, GATE_STEP) in events
    assert (DagsterEventType.STEP_START, DBT_STEP) not in events
    assert _DBT_CALLS == []


def test_passing_gate_means_dbt_runs_after_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is not a wall: once it passes, dbt still builds.

    The in-process executor runs one step at a time, so the order here would hold
    even without the edge; the multiprocess executor that ran e1dd41e5 honours
    only the edge, which test_research_trading_day_orders_dbt_after_the_gate pins.
    """
    _stub_gate(monkeypatch, eod_verdict="healthy")
    result, events = _run(_dbt_assets())

    assert result.success
    gate_done = _index(events, DagsterEventType.STEP_SUCCESS, GATE_STEP)
    dbt_start = _index(events, DagsterEventType.STEP_START, DBT_STEP)
    assert gate_done < dbt_start
    assert _DBT_CALLS == [["build"]]
