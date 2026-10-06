"""The suggestion ledger issues the session's Pine signals, so it runs after engines/pine (S4).

An edge, not a habit of timing: ``deps`` on the asset is what orders the steps in
``research_trading_day`` (see test_trading_day_edges for what timing alone did).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("dagster")
pytest.importorskip("dagster_dbt")

from dagster import AssetKey

PINE = AssetKey(["engines", "pine"])
LEDGER = AssetKey(["engines", "suggestion_ledger"])


def _defs():
    from bifrost_research.orchestration.definitions import build_definitions

    with patch("bifrost_research.orchestration.dbt_assets.dbt_manifest_exists", return_value=False):
        return build_definitions()


def test_the_ledger_has_an_edge_from_the_pine_build() -> None:
    graph = _defs().get_repository_def().asset_graph
    parents = graph.get(LEDGER).parent_keys
    assert PINE in parents
    assert AssetKey(["engines", "volatility"]) in parents
    assert AssetKey(["batch", "husbandry_gate"]) in parents
    # And nothing downstream of the ledger feeds the Pine build (no cycle in disguise).
    assert LEDGER not in graph.get(PINE).parent_keys


def test_both_run_in_the_trading_day_job() -> None:
    job = _defs().get_job_def("research_trading_day")
    keys = job.asset_layer.executable_asset_keys
    assert PINE in keys and LEDGER in keys
