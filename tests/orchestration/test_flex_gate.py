"""batch/flex_gate gates exactly the assets that read Flex data (TD-192).

One gate used to judge Market and Flex together, so an IB Flex failure ([1003]
on 2026-09-08 and 09-16) blocked dbt, SEPA and every engine, none of which read a
Flex row, and those nights' SEPA was lost for good. Now the Market gate
(``husbandry_gate``) gates everything and ``flex_gate`` gates only the Flex
readers. Both directions are held here, with the readers derived from the code:

- every source file that reads Flex-derived data (a ``raw_broker.`` table, or the
  Trade API's ``/executions`` routes, whose book is the Flex-confirmed
  executions_final) is classified in ``FLEX_READERS``;
- every reader that runs in research_trading_day waits for flex_gate, and every
  asset that waits for flex_gate is such a reader (or a dbt model on a
  ``FLEX_DERIVED_SCHEMAS`` source);
- flex_gate is no ancestor of sepa_projection, dbt or the engines.

The gate itself keeps TD-94's design: it fails closed.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("dagster")

from dagster import AssetKey, build_asset_context

from bifrost_research.orchestration import plugin_batch_assets as pba

SRC = Path(pba.__file__).resolve().parents[1]
FLEX_GATE = AssetKey(["batch", "flex_gate"])
MARKET_GATE = AssetKey(["batch", "husbandry_gate"])
SEPA = AssetKey(["features", "sepa_projection"])

#: A read of Flex-derived data: a raw_broker table, or a Trade API executions route.
FLEX_READ = re.compile(r"\braw_broker\.|[\"']/executions\b")


# --- the gate: fails closed on Flex -----------------------------------------


def _flex_ok() -> dict[str, Any]:
    recent = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"dimensions": [{"kind": "flex-trades", "last_ok": True, "last_success_at": recent}]}


def _md(result: Any, key: str) -> Any:
    v = result.metadata[key]
    return getattr(v, "value", v)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    flex: dict[str, Any] | Exception | None = None,
    summary: dict[str, Any] | Exception | None = None,
    config: pba.FlexGateConfig | None = None,
) -> tuple[Any, list[str]]:
    calls: list[str] = []

    def answer(value: Any) -> dict[str, Any]:
        if isinstance(value, Exception):
            raise value
        return value

    def fake_get(url: str, **_kw: Any) -> dict[str, Any]:
        calls.append(url)
        if "/market/" in url:
            raise AssertionError(f"flex_gate probed the Market plugin: {url}")
        if url.endswith("/flex/config/summary"):
            return answer(summary if summary is not None else {"source": "secret"})
        return answer(flex if flex is not None else _flex_ok())

    monkeypatch.setattr(pba, "get_json", fake_get)
    monkeypatch.setattr(pba, "PROBE_RETRY_SEC", 0.0)
    return pba.flex_gate(build_asset_context(), config or pba.FlexGateConfig()), calls


def test_a_fresh_flex_ingest_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    result, calls = _run(monkeypatch)
    assert _md(result, "gate") == "pass" and _md(result, "flex_ingest") == "ok"
    assert any(u.endswith("/flex/dashboard/freshness-kpis") for u in calls)


def test_a_failed_flex_ingest_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 09-08 / 09-16 nights: IB answered [1003]."""
    failed = {"dimensions": [{"kind": "flex-trades", "last_ok": False, "last_error": "[1003]"}]}
    with pytest.raises(RuntimeError, match="flex_gate: Flex ingest failed"):
        _run(monkeypatch, flex=failed)


def test_a_stale_flex_ingest_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = {"dimensions": [{"kind": "flex-trades", "last_ok": True, "last_success_at": old}]}
    with pytest.raises(RuntimeError, match="flex_gate: Flex ingest stale"):
        _run(monkeypatch, flex=stale)


def test_no_flex_token_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="source=none"):
        _run(monkeypatch, summary={"source": "none"})


def test_every_flex_probe_failing_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """TD-94 (a), Flex half: 'unknown' must not pass."""
    down = ConnectionError("connection refused")
    with pytest.raises(RuntimeError, match="flex_gate: fails closed") as err:
        _run(monkeypatch, flex=down, summary=down)
    assert "Flex ingest unknown" in str(err.value)
    assert "Flex token source unknown" in str(err.value)


def test_flex_without_dimensions_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="Flex ingest unknown"):
        _run(monkeypatch, flex={"dimensions": []})


def test_a_transient_flex_probe_failure_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def flaky() -> dict[str, Any]:
        attempts.append("flex")
        if len(attempts) == 1:
            raise ConnectionError("blip")
        return _flex_ok()

    monkeypatch.setattr(pba, "PROBE_RETRY_SEC", 0.0)
    monkeypatch.setattr(
        pba,
        "get_json",
        lambda url, **_kw: {"source": "secret"} if url.endswith("/summary") else flaky(),
    )
    result = pba.flex_gate(build_asset_context(), pba.FlexGateConfig())
    assert _md(result, "gate") == "pass" and len(attempts) == 2


def test_a_hand_run_may_let_unknown_flex_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    down = ConnectionError("connection refused")
    result, _ = _run(
        monkeypatch, flex=down, summary=down, config=pba.FlexGateConfig(allow_unknown=True)
    )
    assert _md(result, "gate") == "pass (allow_unknown)"
    assert "Flex ingest unknown" in _md(result, "overridden")


# --- the readers: derived from the code --------------------------------------


def _flex_reading_files() -> set[str]:
    return {
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if FLEX_READ.search(path.read_text(encoding="utf-8"))
    }


def _defs() -> Any:
    from bifrost_research.orchestration.definitions import build_definitions

    return build_definitions()


def _trading_day(defs: Any) -> set[AssetKey]:
    from bifrost_research.orchestration.schedules import research_trading_day_job

    return research_trading_day_job.selection.resolve(defs.resolve_asset_graph())


def test_the_marker_finds_the_known_readers() -> None:
    """Guard the scan itself: if it stops matching, the classification test is vacuous."""
    found = _flex_reading_files()
    assert "engines/option_pinned/entry.py" in found
    assert "engines/journal_distill.py" in found


def test_every_flex_reading_file_is_classified() -> None:
    found = _flex_reading_files()
    unclassified = sorted(found - set(pba.FLEX_READERS))
    assert not unclassified, (
        f"these read Flex data but are not in plugin_batch_assets.FLEX_READERS: {unclassified}. "
        "Name the asset that runs them (it must then depend on batch/flex_gate) or say why "
        "it is not gated."
    )
    stale = sorted(set(pba.FLEX_READERS) - found)
    assert not stale, f"FLEX_READERS lists files that no longer read Flex data: {stale}"


def test_every_trading_day_flex_reader_waits_for_flex_gate() -> None:
    defs = _defs()
    graph = defs.resolve_asset_graph()
    trading_day = _trading_day(defs)
    assert FLEX_GATE in trading_day and MARKET_GATE in trading_day
    for path, (asset, why) in pba.FLEX_READERS.items():
        if asset is None:
            # The reason names the asset (or says it is not one); it must really be
            # outside the batch, else it would race the Flex ingest ungated.
            named = why.split(":", 1)[0]
            if "/" in named:
                assert AssetKey(named.split("/")) not in trading_day, (path, named)
            continue
        key = AssetKey(asset.split("/"))
        assert key in trading_day, f"{asset} ({path}) is not in research_trading_day"
        assert FLEX_GATE in graph.get(key).parent_keys, f"{asset} reads Flex data ungated"


def test_only_flex_readers_wait_for_flex_gate() -> None:
    defs = _defs()
    graph = defs.resolve_asset_graph()
    readers = {AssetKey(a.split("/")) for a, _ in pba.FLEX_READERS.values() if a}
    children = {k for k in graph.get_all_asset_keys() if FLEX_GATE in graph.get(k).parent_keys}
    assert children == readers, sorted(k.to_user_string() for k in children ^ readers)


def test_flex_gate_is_not_upstream_of_sepa_dbt_or_the_engines() -> None:
    """The edges a Flex failure travelled to SEPA on 09-08 and 09-16 are gone."""
    graph = _defs().resolve_asset_graph()  # with the dbt assets when the manifest exists
    downstream: set[AssetKey] = set()
    frontier = [FLEX_GATE]
    while frontier:
        for k in graph.get(frontier.pop()).child_keys:
            if k not in downstream:
                downstream.add(k)
                frontier.append(k)
    readers = {AssetKey(a.split("/")) for a, _ in pba.FLEX_READERS.values() if a}
    assert SEPA not in downstream
    assert downstream <= readers, sorted(k.to_user_string() for k in downstream - readers)
    # ...while the Market gate still gates them.
    assert MARKET_GATE in graph.get(SEPA).parent_keys


def test_a_dbt_source_in_a_flex_schema_would_wait_for_flex_gate() -> None:
    """dbt is one step: one Flex-derived source puts the whole build behind flex_gate."""
    from bifrost_research.orchestration.dbt_assets import _engine_writers

    manifest = {
        "sources": {
            "source.x.broker.executions_final": {
                "source_name": "broker",
                "name": "executions_final",
                "schema": "raw_broker",
            },
            "source.x.market.stock_daily": {
                "source_name": "market",
                "name": "stock_daily",
                "schema": "raw_market",
            },
        }
    }
    flex_model = {"depends_on": {"nodes": ["source.x.broker.executions_final"]}}
    market_model = {"depends_on": {"nodes": ["source.x.market.stock_daily"]}}
    assert pba.flex_gate.key in _engine_writers(manifest, flex_model)
    assert pba.flex_gate.key not in _engine_writers(manifest, market_model)


def test_no_dbt_source_reads_a_flex_schema_today() -> None:
    """If one ever does, dbt (and SEPA behind it) waits for flex_gate again: say so."""
    from bifrost_research.orchestration.paths import DBT_MANIFEST_PATH

    if not DBT_MANIFEST_PATH.is_file():
        pytest.skip("dbt target/manifest.json not present — run `make dbt-parse` first")
    manifest = json.loads(DBT_MANIFEST_PATH.read_text())
    flex_sources = sorted(
        uid for uid, s in manifest["sources"].items() if s.get("schema") in pba.FLEX_DERIVED_SCHEMAS
    )
    assert flex_sources == []


def test_the_flex_gate_is_in_the_trading_day_job_without_the_enqueues() -> None:
    with patch(
        "bifrost_research.orchestration.dbt_assets.dbt_manifest_exists", return_value=False
    ):
        defs = _defs()
    trading_day = _trading_day(defs)
    assert FLEX_GATE in trading_day
    assert AssetKey(["batch", "flex_trades"]) not in trading_day
