"""dbt waits for the husbandry gate, and sepa_projection waits for dbt (Owner 2026-09-29).

research_trading_day run e1dd41e5 (2026-09-29 02:30 UTC): ``bifrost_research_dbt_assets``
logged STEP_START at 02:30:21 and ``batch__husbandry_gate`` at 02:30:37 — dbt ran
beside the gate instead of after it. The default dagster-dbt translator gave the dbt
assets only their dbt sources as upstreams, so a failing gate blocked the engines
but not dbt (09-09, 09-10 and 09-17 each ran dbt to success after the gate failed).

The same run had no edge from dbt to ``features/sepa_projection`` either: the mart it
projects landed at 02:33:29 and the projection read it at 02:36:21 by luck.

These tests run the real dbt assets in-process with a stub dbt CLI. They need the
parsed manifest (``make dbt-parse``); skipped without it, like the Definitions
smoke test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("dagster")
pytest.importorskip("dagster_dbt")

from dagster import (
    AssetCheckResult,
    AssetKey,
    DagsterEventType,
    MaterializeResult,
    materialize,
)
from dagster_dbt import DbtCliResource

from bifrost_research.orchestration import plugin_batch_assets as pba
from bifrost_research.orchestration import sepa_projection_asset as spa
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
FEATURE_MART = AssetKey(["mart_sepa_feature_daily"])
GATE_STEP = "batch__husbandry_gate"
DBT_STEP = "bifrost_research_dbt_assets"
SEPA_STEP = "features__sepa_projection"

Events = list[tuple[DagsterEventType, str | None]]

# dbt invocations seen by the stub; the resource is a frozen pydantic model.
_DBT_CALLS: list[list[str]] = []


class _Invocation:
    """Emits what ``dbt build`` would for the SEPA feature mart, or fails.

    dbt's outputs are all optional, so the other models need not appear. The mart
    is followed by its dbt tests, which are blocking checks: sepa_projection waits
    for them as well as for the materialization.
    """

    def __init__(self, context: Any, fail: bool) -> None:
        self._context = context
        self._fail = fail

    def stream(self) -> Any:
        if self._fail:
            raise RuntimeError("dbt build failed (stub)")
        if FEATURE_MART not in self._context.selected_asset_keys:
            return
        yield MaterializeResult(asset_key=FEATURE_MART)
        for check in self._context.selected_asset_check_keys:
            if check.asset_key == FEATURE_MART:
                yield AssetCheckResult(asset_key=FEATURE_MART, check_name=check.name, passed=True)


class StubDbtCli(DbtCliResource):
    """Records the invocation instead of shelling out to dbt."""

    fail: bool = False

    def cli(self, args: Any, *, context: Any = None, **_kw: Any) -> Any:  # type: ignore[override]
        _DBT_CALLS.append(list(args))
        return _Invocation(context, self.fail)


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


def _stub_projection(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the database side of sepa_projection; returns what it projected."""
    projected: list[str] = []

    def fake_projection(_conn: Any) -> dict[str, Any]:
        projected.append("mart_sepa_feature_daily")
        return {"rows": 0}

    monkeypatch.setattr(spa, "run_sepa_projection", fake_projection)
    monkeypatch.setattr(
        "bifrost_research.db.conn.connect", lambda: SimpleNamespace(close=lambda: None)
    )
    return projected


def _dbt_assets() -> list[Any]:
    from bifrost_research.orchestration.dbt_assets import load_dbt_assets

    assets = load_dbt_assets()
    assert assets, "manifest present but no dbt assets loaded"
    return assets


def _dbt_keys() -> set[AssetKey]:
    return {k for a in _dbt_assets() for k in a.keys}


def _run(*assets: Any, dbt_fails: bool = False) -> tuple[Any, Events]:
    _DBT_CALLS.clear()
    dbt = StubDbtCli(
        project_dir=str(DBT_PROJECT_DIR), profiles_dir=str(DBT_PROFILES_DIR), fail=dbt_fails
    )
    result = materialize(
        [pba.husbandry_gate, *assets], resources={"dbt": dbt}, raise_on_error=False
    )
    events = [(e.event_type, e.step_key) for e in result.all_events]
    return result, events


def _index(events: Events, kind: DagsterEventType, step: str) -> int:
    return events.index((kind, step))


def _trading_day_upstream(node_name: str) -> set[str]:
    """In-job upstream nodes of ``node_name`` in research_trading_day.

    A dep on an asset with blocking checks is one input fed by several outputs.
    """
    from bifrost_research.orchestration.definitions import defs

    job = defs.resolve_job_def("research_trading_day")
    return {
        upstream.node
        for node, inputs in job.dependencies.items()
        if node.name == node_name
        for dep in inputs.values()
        for upstream in dep.get_node_dependencies()
    }


def test_every_dbt_asset_has_the_gate_as_parent() -> None:
    from bifrost_research.orchestration.definitions import defs

    graph = defs.resolve_asset_graph()
    dbt_keys = _dbt_keys()
    ungated = sorted(k.to_user_string() for k in dbt_keys if GATE not in graph.get(k).parent_keys)
    assert not ungated, f"{len(ungated)} dbt assets do not wait for the gate: {ungated[:5]}"
    # The source-derived deps are kept alongside the gate.
    parents = set().union(*(graph.get(k).parent_keys for k in dbt_keys)) - dbt_keys
    assert any(p.path[0] == "market" for p in parents), sorted(parents)


def test_research_trading_day_orders_dbt_after_the_gate() -> None:
    """The job that ran e1dd41e5 selects both, so the edge is in its plan."""
    from bifrost_research.orchestration.definitions import defs

    selected = defs.resolve_job_def("research_trading_day").asset_layer.executable_asset_keys
    assert GATE in selected
    assert _dbt_keys() <= selected
    assert GATE_STEP in _trading_day_upstream(DBT_STEP)


def test_failing_gate_means_dbt_never_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_gate(monkeypatch, eod_verdict="critical")
    result, events = _run(*_dbt_assets())

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
    result, events = _run(*_dbt_assets())

    assert result.success
    gate_done = _index(events, DagsterEventType.STEP_SUCCESS, GATE_STEP)
    dbt_start = _index(events, DagsterEventType.STEP_START, DBT_STEP)
    assert gate_done < dbt_start
    assert _DBT_CALLS == [["build"]]


def test_sepa_projection_depends_on_the_mart_dbt_builds() -> None:
    from bifrost_research.orchestration.definitions import defs

    # The key must be the dbt asset itself, not an external stub of the same name.
    assert FEATURE_MART in _dbt_keys()
    parents = defs.resolve_asset_graph().get(spa.sepa_projection.key).parent_keys
    assert {GATE, FEATURE_MART} <= parents
    assert DBT_STEP in _trading_day_upstream(SEPA_STEP)


def test_failing_dbt_means_sepa_projection_never_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-built mart must not be stamped as today's projection."""
    _stub_gate(monkeypatch, eod_verdict="healthy")
    projected = _stub_projection(monkeypatch)
    result, events = _run(*_dbt_assets(), spa.sepa_projection, dbt_fails=True)

    assert not result.success
    assert (DagsterEventType.STEP_FAILURE, DBT_STEP) in events
    assert (DagsterEventType.STEP_START, SEPA_STEP) not in events
    assert projected == []


def test_sepa_projection_runs_after_dbt(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_gate(monkeypatch, eod_verdict="healthy")
    projected = _stub_projection(monkeypatch)
    result, events = _run(*_dbt_assets(), spa.sepa_projection)

    assert result.success
    dbt_done = _index(events, DagsterEventType.STEP_SUCCESS, DBT_STEP)
    sepa_start = _index(events, DagsterEventType.STEP_START, SEPA_STEP)
    assert dbt_done < sepa_start
    assert projected == ["mart_sepa_feature_daily"]
