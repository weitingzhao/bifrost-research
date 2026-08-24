"""Dagster assets for Python research engines.

Dependency chain (documented):
  Plugin market ingest (external) → dbt transforms → Python analytics → AI forecast

Wave 5.1 registers engines as assets wrapping scheduler / module entrypoints.
D10 BLOCKED — no trade execution.

Note: do not use ``from __future__ import annotations`` here — Dagster validates
``context`` type hints at definition time and needs the live class object.
"""

from typing import Any

from dagster import AssetExecutionContext, AssetKey, AssetSpec, MaterializeResult, asset

from bifrost_research.orchestration import runners

# External dependency: Market Data Plugin writes market.* (not owned by Research).
plugin_market_ingest = AssetSpec(
    key=AssetKey(["external", "plugin_market_ingest"]),
    description=(
        "External: Market Data Plugin Polygon ingest → market.* on "
        "bifrost_golden_source. Research reads only; Plugin owns writes."
    ),
    group_name="external",
)

_MARKET = AssetKey(["external", "plugin_market_ingest"])


def _metadata(result: dict[str, Any]) -> dict[str, Any]:
    """Flatten simple result fields for Dagster materialization metadata."""
    out: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
        elif value is None:
            continue
        else:
            out[key] = str(value)[:500]
    out["advisory"] = "D10 BLOCKED"
    return out


@asset(
    key=AssetKey(["engines", "volatility"]),
    deps=[_MARKET],
    group_name="python_analytics",
    description="Volatility engines (max pain / ATM IV / PCR / IV percentile) → features.option_metric_*_daily",
)
def volatility(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_volatility()
    context.log.info("volatility result=%s", result)
    return MaterializeResult(metadata=_metadata(result))


@asset(
    key=AssetKey(["engines", "momentum"]),
    deps=[_MARKET],
    group_name="python_analytics",
    description="Momentum Radar → features.stock_signal_momentum_daily",
)
def momentum(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_momentum()
    context.log.info("momentum result=%s", result)
    return MaterializeResult(metadata=_metadata(result))


@asset(
    key=AssetKey(["engines", "gex"]),
    deps=[_MARKET],
    group_name="python_analytics",
    description="GEX Engine → features.option_metric_gex_daily / option_metric_gex_levels_daily",
)
def gex(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_gex()
    context.log.info("gex result=%s", result)
    return MaterializeResult(metadata=_metadata(result))


@asset(
    key=AssetKey(["engines", "surface"]),
    deps=[_MARKET],
    group_name="python_analytics",
    description="IV Surface / vol cone → features.option_surface_iv_daily",
)
def surface(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_surface()
    context.log.info("surface result=%s", result)
    return MaterializeResult(metadata=_metadata(result))


@asset(
    key=AssetKey(["engines", "flow"]),
    deps=[_MARKET],
    group_name="python_analytics",
    description="Order Flow / sentiment → features.option_flow_sentiment_daily",
)
def flow(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_flow()
    context.log.info("flow result=%s", result)
    return MaterializeResult(metadata=_metadata(result))


@asset(
    key=AssetKey(["engines", "terrain"]),
    deps=[
        AssetKey(["engines", "volatility"]),
        AssetKey(["engines", "momentum"]),
        AssetKey(["engines", "gex"]),
        AssetKey(["engines", "surface"]),
    ],
    group_name="python_analytics",
    description="Market terrain / regime → features.stock_forecast_terrain_daily",
)
def terrain(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_terrain()
    context.log.info("terrain result=%s", result)
    return MaterializeResult(metadata=_metadata(result))


@asset(
    key=AssetKey(["engines", "forecast"]),
    deps=[AssetKey(["engines", "terrain"])],
    group_name="ai_forecast",
    description="AI intraday playbook / path calls → features.stock_forecast_session / stock_forecast_hourly",
)
def forecast(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_forecast()
    context.log.info("forecast result=%s", result)
    return MaterializeResult(metadata=_metadata(result))


@asset(
    key=AssetKey(["engines", "event_radar"]),
    deps=[_MARKET],
    group_name="ai_forecast",
    description="Event Radar 5-step pipeline → features.event_signal_radar_daily",
)
def event_radar(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_event_radar()
    context.log.info("event_radar result=%s", result)
    return MaterializeResult(metadata=_metadata(result))


@asset(
    key=AssetKey(["engines", "backtest"]),
    deps=[AssetKey(["engines", "forecast"])],
    group_name="ai_forecast",
    description="Forecast settlement / accuracy → features.stock_backtest_settlement / stock_backtest_results_period",
)
def backtest(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_backtest()
    context.log.info("backtest result=%s", result)
    return MaterializeResult(metadata=_metadata(result))


ENGINE_ASSETS = [
    volatility,
    momentum,
    gex,
    surface,
    flow,
    terrain,
    forecast,
    event_radar,
    backtest,
]
