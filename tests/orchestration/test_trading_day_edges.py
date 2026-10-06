"""research_trading_day orders its steps by edges, not by timing (Owner 2026-09-29).

gate -> dbt: in run e1dd41e5 (2026-09-29 02:30 UTC) ``bifrost_research_dbt_assets``
logged STEP_START at 02:30:21 and ``batch__husbandry_gate`` at 02:30:37. The default
dagster-dbt translator gave the dbt assets only their dbt sources as upstreams, so a
failing gate blocked the engines but not dbt (09-09, 09-10 and 09-17 each ran dbt to
success after the gate failed).

dbt -> sepa_projection: the mart it projects landed at 02:33:29 and the projection
read it at 02:36:21 by luck.

volatility / gex -> dbt: mart_sepa_tier_options was built at 02:31:15 from the
previous session's IV percentile, PCR and GEX levels; the engines wrote that night's
rows at 02:33-02:36.

gate -> signal_hit_fwd_fill: its only upstream is outside the job, so it started at
02:30:21, before the gate had judged the session whose bars it reads.

These tests run the real dbt assets in-process with a stub dbt CLI and stub engine
runners. They need the parsed manifest (``make dbt-parse``); skipped without it,
like the Definitions smoke test.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
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

from bifrost_research.orchestration import engine_assets as ea
from bifrost_research.orchestration import plugin_batch_assets as pba
from bifrost_research.orchestration import runners
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
VOL_STEP = "engines__volatility"
GEX_STEP = "engines__gex"
FWD_FILL_STEP = "engines__signal_hit_fwd_fill"

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
    monkeypatch.setattr(pba, "expected_session", lambda: date(2026, 9, 28))


def _stub_projection(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the database side of sepa_projection; returns what it projected."""
    projected: list[str] = []

    def fake_projection(_conn: Any, **_kw: Any) -> dict[str, Any]:
        projected.append("mart_sepa_feature_daily")
        return {"rows": 0}

    monkeypatch.setattr(spa, "run_sepa_projection", fake_projection)
    monkeypatch.setattr(
        "bifrost_research.db.calendar.latest_closed_session", lambda _conn, **_kw: date(2026, 10, 5)
    )
    monkeypatch.setattr(
        "bifrost_research.db.conn.connect", lambda: SimpleNamespace(close=lambda: None)
    )
    return projected


def _stub_engines(monkeypatch: pytest.MonkeyPatch, *, failing: str | None = None) -> list[str]:
    """Replace the engine runners the assets call; returns the engines that ran."""
    ran: list[str] = []

    def runner(name: str) -> Any:
        def run(**_kw: Any) -> dict[str, Any]:
            ran.append(name)
            if name == failing:
                raise RuntimeError(f"{name} failed (stub)")
            return {"engine": name}

        return run

    for name in ("volatility", "gex", "signal_hit_fwd_fill"):
        monkeypatch.setattr(runners, f"run_{name}", runner(name))
    return ran


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


def test_every_dbt_source_is_ingested_or_has_a_writer() -> None:
    """A dbt model reading a table some engine writes must wait for that engine.

    market.* is written by the Plugin and judged by the gate; every other source
    needs an entry in ENGINE_WRITTEN_SOURCES, or dbt races the engine again.
    """
    from bifrost_research.orchestration.dbt_assets import ENGINE_WRITTEN_SOURCES

    manifest = json.loads(DBT_MANIFEST_PATH.read_text())
    read = {d for n in manifest["nodes"].values() for d in n.get("depends_on", {}).get("nodes", [])}
    sources = {
        (s["source_name"], s["name"]) for uid, s in manifest["sources"].items() if uid in read
    }
    engine_written = {s for s in sources if s[0] != "market"}
    assert engine_written, "no engine-written source found; the check is looking in the wrong place"
    unmapped = sorted(engine_written - set(ENGINE_WRITTEN_SOURCES))
    assert not unmapped, f"dbt reads these without waiting for their writer: {unmapped}"


def test_dbt_waits_for_the_engines_it_reads() -> None:
    from bifrost_research.orchestration.definitions import defs

    parents = defs.resolve_asset_graph().get(AssetKey(["mart_sepa_tier_options"])).parent_keys
    assert {GATE, ea.volatility.key, ea.gex.key} <= parents
    assert {GATE_STEP, VOL_STEP, GEX_STEP} <= _trading_day_upstream(DBT_STEP)


def test_failing_engine_means_dbt_never_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Better no rebuild than a SEPA options tier built from the previous session."""
    _stub_gate(monkeypatch, eod_verdict="healthy")
    ran = _stub_engines(monkeypatch, failing="volatility")
    result, events = _run(ea.volatility, ea.gex, *_dbt_assets())

    assert not result.success
    assert (DagsterEventType.STEP_FAILURE, VOL_STEP) in events
    assert (DagsterEventType.STEP_START, DBT_STEP) not in events
    assert _DBT_CALLS == []
    assert "volatility" in ran


def test_dbt_runs_after_the_engines_it_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_gate(monkeypatch, eod_verdict="healthy")
    _stub_engines(monkeypatch)
    result, events = _run(ea.volatility, ea.gex, *_dbt_assets())

    assert result.success
    dbt_start = _index(events, DagsterEventType.STEP_START, DBT_STEP)
    assert _index(events, DagsterEventType.STEP_SUCCESS, VOL_STEP) < dbt_start
    assert _index(events, DagsterEventType.STEP_SUCCESS, GEX_STEP) < dbt_start
    assert _DBT_CALLS == [["build"]]


def test_signal_hit_fwd_fill_waits_for_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    assert GATE_STEP in _trading_day_upstream(FWD_FILL_STEP)

    _stub_gate(monkeypatch, eod_verdict="critical")
    ran = _stub_engines(monkeypatch)
    result, events = _run(ea.signal_hit_fwd_fill)

    assert not result.success
    assert (DagsterEventType.STEP_START, FWD_FILL_STEP) not in events
    assert ran == []
