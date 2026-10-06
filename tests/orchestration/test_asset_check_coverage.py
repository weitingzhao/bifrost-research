"""Ratchet (TD-92): every asset has an output check, or says why not.

An asset whose runner swallowed its failures reported SUCCESS for weeks (gex
intraday 646-669/669 failed, three green weeks; option_pinned 0 rows, TD-89). A
new asset must now declare a check — ``check_specs=output_check_specs(key)`` with
an ``OutputSpec``, or a separate ``@asset_check`` registered in Definitions — or
be listed in ``asset_checks.OUTPUT_CHECK_OPT_OUT`` with a reason.
"""

from __future__ import annotations

import pytest

pytest.importorskip("dagster")

from dagster import AssetKey

from bifrost_research.orchestration.asset_checks import (
    OUTPUT_CHECK_OPT_OUT,
    opt_out_reason,
)


def _covered_assets() -> list:
    from bifrost_research.orchestration.engine_assets import ENGINE_ASSETS
    from bifrost_research.orchestration.market_self_heal import market_self_heal
    from bifrost_research.orchestration.market_slot_schedules import MARKET_SCHEDULE_ASSETS
    from bifrost_research.orchestration.plugin_batch_assets import PLUGIN_BATCH_ASSETS
    from bifrost_research.orchestration.research_aux_schedules import RESEARCH_AUX_ASSETS
    from bifrost_research.orchestration.sepa_projection_asset import SEPA_PROJECTION_ASSETS

    return [
        *ENGINE_ASSETS,
        *RESEARCH_AUX_ASSETS,
        *PLUGIN_BATCH_ASSETS,
        *MARKET_SCHEDULE_ASSETS,
        market_self_heal,
        *SEPA_PROJECTION_ASSETS,
    ]


def _checked_keys() -> set[AssetKey]:
    from bifrost_research.orchestration.definitions import defs

    graph = defs.resolve_asset_graph()
    return {ck.asset_key for ck in graph.asset_check_keys}


def test_every_asset_has_an_output_check_or_a_reason() -> None:
    checked = _checked_keys()
    missing = sorted(
        a.key.to_user_string()
        for a in _covered_assets()
        if a.key not in checked and not opt_out_reason(a.key)
    )
    assert not missing, (
        f"{len(missing)} assets have no output check and no opt-out reason: {missing}. "
        "Add check_specs=output_check_specs(key) with an OutputSpec, or an entry in "
        "asset_checks.OUTPUT_CHECK_OPT_OUT saying why the asset cannot be judged."
    )


def test_opt_outs_name_real_assets_without_checks() -> None:
    """A stale opt-out would hide the next asset given that key."""
    keys = {a.key.to_user_string() for a in _covered_assets()}
    checked = {k.to_user_string() for k in _checked_keys()}
    stale = sorted(k for k in OUTPUT_CHECK_OPT_OUT if k not in keys)
    assert not stale, f"opt-outs for assets that do not exist: {stale}"
    both = sorted(k for k in OUTPUT_CHECK_OPT_OUT if k in checked)
    assert not both, f"opted out but also checked: {both}"
    assert all(len(r) > 20 for r in OUTPUT_CHECK_OPT_OUT.values()), "a reason, not a word"


def test_the_engines_research_trading_day_runs_are_all_checked() -> None:
    """The nightly batch is where green-while-wrong hid; no opt-out among its engines
    beyond the scaffold."""
    from bifrost_research.orchestration.definitions import defs

    selected = defs.resolve_job_def("research_trading_day").asset_layer.executable_asset_keys
    checked = _checked_keys()
    engines = {k for k in selected if k.path[0] == "engines"}
    assert engines, "research_trading_day selects no engines"
    unchecked = sorted(k.to_user_string() for k in engines - checked)
    assert unchecked == [], unchecked
