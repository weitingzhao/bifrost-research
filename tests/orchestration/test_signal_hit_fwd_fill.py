"""The 20-day hit rate has to be fillable at all.

`signal_hit.entry.run` walks the last `lookback_days` trading days and needs
`horizon + 1` bars at or after each trade date. The scheduled run passes 3, so
`hit_20d` was written NULL on every row and nothing ever came back for it —
`policy.min_hit_rate` was gating candidates on a column that could not be
populated. These tests pin the two things that make the late-fill run: a window
wide enough to clear the longest horizon, and a group the trading-day job
actually selects.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bifrost_research.orchestration import runners

LONGEST_HORIZON_SESSIONS = 20


def test_the_window_clears_the_longest_horizon() -> None:
    import inspect

    default = inspect.signature(runners.run_signal_hit_fwd_fill).parameters["lookback_days"].default
    assert default > LONGEST_HORIZON_SESSIONS


def test_the_fill_is_the_signal_hit_engine_on_a_wider_window() -> None:
    with patch("bifrost_research.engines.signal_hit.entry.run", return_value={"rows": 7}) as run:
        out = runners.run_signal_hit_fwd_fill(lookback_days=45)

    assert run.call_args.kwargs == {"lookback_days": 45}
    assert out["rows"] == 7
    assert out["engine"] == "signal_hit_fwd_fill"


def test_the_fill_runs_on_a_trading_day() -> None:
    """A `research_signals` group would put it in the one job that excludes it."""
    pytest.importorskip("dagster")
    from dagster import AssetKey, AssetsDefinition, SourceAsset

    from bifrost_research.orchestration.engine_assets import signal_hit_fwd_fill
    from bifrost_research.orchestration.schedules import research_trading_day_job

    with patch(
        "bifrost_research.orchestration.dbt_assets.dbt_manifest_exists",
        return_value=False,
    ):
        from bifrost_research.orchestration.definitions import build_definitions

        defs = build_definitions()

    resolvable = [a for a in defs.assets if isinstance(a, (AssetsDefinition, SourceAsset))]
    selected = research_trading_day_job.selection.resolve(resolvable)

    assert AssetKey(["engines", "signal_hit_fwd_fill"]) in selected
    assert signal_hit_fwd_fill.group_names_by_key == {
        AssetKey(["engines", "signal_hit_fwd_fill"]): "python_analytics"
    }
