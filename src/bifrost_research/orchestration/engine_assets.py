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
from bifrost_research.orchestration.asset_checks import (
    ERROR,
    OutputSpec,
    field,
    judged_materialization,
    output_check_specs,
)

# External dependency: Market Data Plugin writes market.* (not owned by Research).
plugin_market_ingest = AssetSpec(
    key=AssetKey(["external", "plugin_market_ingest"]),
    description=(
        "External: Market Data Plugin Polygon ingest → market.* / raw_market.* on "
        "bifrost_golden_source. Research reads only; Plugin owns writes."
    ),
    group_name="external",
)

# Batch enqueue assets (Dagster schedules; workers still execute).
_MARKET_EOD = AssetKey(["batch", "market_eod"])
_GATE = AssetKey(["batch", "husbandry_gate"])
_SEPA = AssetKey(["features", "sepa_projection"])
_MARKET = [_MARKET_EOD, _GATE]


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


# --- Output checks (TD-92): how each runner's result is judged -----------------

_VOL_ROW_FIELDS = ("rows_written", "atm_rows_written", "pcr_rows_written")


def volatility_rows(result: dict[str, Any]) -> int | None:
    """Rows over the volatility slots (max-pain, atm-iv-pcr, iv-percentile)."""
    slots = result.get("slots")
    if not isinstance(slots, list):
        return None
    return sum(int(s.get(f) or 0) for s in slots if isinstance(s, dict) for f in _VOL_ROW_FIELDS)


def volatility_skips(result: dict[str, Any]) -> list[tuple[Any, str]]:
    return [
        (ERROR, f"slot {s.get('slot')} skipped: {s.get('reason') or 'no reason given'}")
        for s in result.get("slots") or []
        if isinstance(s, dict) and s.get("skipped") is True
    ]


VOLATILITY_SPEC = OutputSpec(rows=volatility_rows, expect_rows=True, extra=volatility_skips)

# Engines that rewrite a window of sessions every run (vrp, momentum, canonical_pnl,
# scan, signal_hit_fwd_fill): zero rows cannot be a quiet day.
WINDOW_ROWS_SPEC = OutputSpec(rows=field("rows_written"), expect_rows=True)
# Zero is a legitimate answer (no horizon elapsed, no option leg held): recorded,
# judged only for errors and skips.
QUIET_ROWS_SPEC = OutputSpec(rows=field("rows_written"), trailing=False)
VRP_FWD_SPEC = OutputSpec(rows=field("updated"), trailing=False)
# The universe is never empty; ``written`` counts the rows the rule kept.
UNIVERSE_SPEC = OutputSpec(rows=field("written"), expect_rows=True)
# A name without a spot is skipped; no normal share is measured yet, so WARN.
TERRAIN_SPEC = OutputSpec(
    rows=field("rows_written"),
    expect_rows=True,
    soft_failed=(("skipped_no_spot", "rows_written"),),
)

# gex / iv-surface / flow: one row set per (symbol, session); ~3% of symbol-days
# fail on a normal night (names still onboarding), 90%+ when the engine is broken.
PER_SYMBOL_SPEC = OutputSpec(
    rows=field("rows_written"),
    expect_rows=True,
    failed=(("symbols_failed", "symbols_ok"),),
)

# A broken trigger table fails every emission (TD-113); a normal night fails none.
FORECAST_SPEC = OutputSpec(
    rows=field("rows_written"),
    expect_rows=True,
    failed=(("trigger_failures", "trigger_ok"),),
)


def pine_rows(result: dict[str, Any]) -> int | None:
    scripts = result.get("scripts")
    if not isinstance(scripts, dict):
        return None
    return sum(int(v.get("rows") or 0) for v in scripts.values() if isinstance(v, dict))


def pine_runner_down(result: dict[str, Any]) -> list[tuple[Any, str]]:
    """``error_sample['*']`` is the pine-runner itself failing: that script wrote nothing."""
    return [
        (ERROR, f"pine script {sid}: {v['error_sample']['*']}")
        for sid, v in (result.get("scripts") or {}).items()
        if isinstance(v, dict) and isinstance(v.get("error_sample"), dict) and "*" in v["error_sample"]
    ]


# Pine writes buy / sell sessions only, so a quiet window may hold none, and a
# version rebuild writes years at once: rows are recorded, not judged.
PINE_SPEC = OutputSpec(rows=pine_rows, trailing=False, extra=pine_runner_down)


def suggestion_errors(result: dict[str, Any]) -> list[tuple[Any, str]]:
    """The Pine issue is caught so settlement still runs; its error must still show."""
    return [
        (ERROR, f"{part}: {result[part]['error']}"[:300])
        for part in ("issued", "pine", "settled")
        if isinstance(result.get(part), dict) and result[part].get("error")
    ]


SUGGESTION_SPEC = OutputSpec(extra=suggestion_errors)


@asset(
    key=AssetKey(["engines", "volatility"]),
    check_specs=output_check_specs(AssetKey(["engines", "volatility"])),
    deps=_MARKET,
    group_name="python_analytics",
    description="Volatility engines (max pain / ATM IV / PCR / IV percentile) → features.option_metric_*_daily",
)
def volatility(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_volatility()
    context.log.info("volatility result=%s", result)
    return judged_materialization(context, result, _metadata(result), VOLATILITY_SPEC)


@asset(
    key=AssetKey(["engines", "vrp"]),
    check_specs=output_check_specs(AssetKey(["engines", "vrp"])),
    deps=[AssetKey(["engines", "volatility"])],
    group_name="python_analytics",
    description=(
        "IV-RV spread (VRP) → features.stock_signal_vrp_daily. Runs after volatility so the "
        "day's ATM IV exists; the 23:10 UTC aux schedule used to write the day with IV NULL."
    ),
)
def vrp(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_vrp()
    context.log.info("vrp result=%s", result)
    return judged_materialization(context, result, _metadata(result), WINDOW_ROWS_SPEC)


@asset(
    key=AssetKey(["engines", "vrp_fwd_ret_20d"]),
    check_specs=output_check_specs(AssetKey(["engines", "vrp_fwd_ret_20d"])),
    deps=[AssetKey(["engines", "vrp"])],
    group_name="python_analytics",
    description="fwd_ret_20d backfill on VRP rows whose 20 sessions have elapsed",
)
def vrp_fwd_ret_20d(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_vrp_fwd_ret_20d()
    context.log.info("vrp_fwd_ret_20d result=%s", result)
    return judged_materialization(context, result, _metadata(result), VRP_FWD_SPEC)


@asset(
    key=AssetKey(["engines", "momentum"]),
    check_specs=output_check_specs(AssetKey(["engines", "momentum"])),
    deps=_MARKET,
    group_name="python_analytics",
    description="Momentum Radar → features.stock_signal_momentum_daily",
)
def momentum(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_momentum()
    context.log.info("momentum result=%s", result)
    return judged_materialization(context, result, _metadata(result), WINDOW_ROWS_SPEC)


@asset(
    key=AssetKey(["engines", "gex"]),
    check_specs=output_check_specs(AssetKey(["engines", "gex"])),
    deps=_MARKET,
    group_name="python_analytics",
    description="GEX Engine → features.option_metric_gex_daily / option_metric_gex_levels_daily",
)
def gex(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_gex()
    context.log.info("gex result=%s", result)
    return judged_materialization(context, result, _metadata(result), PER_SYMBOL_SPEC)


@asset(
    key=AssetKey(["engines", "surface"]),
    check_specs=output_check_specs(AssetKey(["engines", "surface"])),
    deps=_MARKET,
    group_name="python_analytics",
    description="IV Surface / vol cone → features.option_surface_iv_daily",
)
def surface(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_surface()
    context.log.info("surface result=%s", result)
    return judged_materialization(context, result, _metadata(result), PER_SYMBOL_SPEC)


@asset(
    key=AssetKey(["engines", "flow"]),
    check_specs=output_check_specs(AssetKey(["engines", "flow"])),
    deps=_MARKET,
    group_name="python_analytics",
    description="Order Flow / sentiment → features.option_flow_sentiment_daily",
)
def flow(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_flow()
    context.log.info("flow result=%s", result)
    return judged_materialization(context, result, _metadata(result), PER_SYMBOL_SPEC)


@asset(
    key=AssetKey(["engines", "terrain"]),
    check_specs=output_check_specs(AssetKey(["engines", "terrain"])),
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
    return judged_materialization(context, result, _metadata(result), TERRAIN_SPEC)


@asset(
    key=AssetKey(["engines", "forecast"]),
    check_specs=output_check_specs(AssetKey(["engines", "forecast"])),
    deps=[AssetKey(["engines", "terrain"])],
    group_name="ai_forecast",
    description="AI intraday playbook / path calls → features.stock_forecast_session / stock_forecast_hourly",
)
def forecast(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_forecast()
    context.log.info("forecast result=%s", result)
    return judged_materialization(context, result, _metadata(result), FORECAST_SPEC)


@asset(
    key=AssetKey(["engines", "event_radar"]),
    deps=_MARKET,
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


@asset(
    key=AssetKey(["engines", "canonical_pnl"]),
    check_specs=output_check_specs(AssetKey(["engines", "canonical_pnl"])),
    deps=_MARKET,
    group_name="python_analytics",
    description=(
        "Canonical structure hypothetical PnL → features.stock_signal_canonical_pnl_daily "
        "+ dw_stock.mart_canonical_pnl_daily (Cron path retained for weekly; Dagster optional)"
    ),
)
def canonical_pnl(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_canonical_pnl(lookback_months=6, dry_run=False)
    context.log.info("canonical_pnl result=%s", result)
    return judged_materialization(context, result, _metadata(result), WINDOW_ROWS_SPEC)


@asset(
    key=AssetKey(["engines", "scan"]),
    check_specs=output_check_specs(AssetKey(["engines", "scan"])),
    deps=[
        AssetKey(["engines", "volatility"]),
        AssetKey(["engines", "gex"]),
        AssetKey(["engines", "surface"]),
        AssetKey(["engines", "terrain"]),
        _SEPA,
    ],
    group_name="python_analytics",
    description=(
        "Materialized multi-lens scanner → features.stock_signal_scan_daily. "
        "OpEx runs on its own Dagster schedule (research_opex); VRP is in this graph."
    ),
)
def scan(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_scan()
    context.log.info("scan result=%s", result)
    return judged_materialization(context, result, _metadata(result), WINDOW_ROWS_SPEC)


@asset(
    key=AssetKey(["engines", "pine"]),
    check_specs=output_check_specs(AssetKey(["engines", "pine"])),
    deps=[AssetKey(["engines", "scan"])],
    group_name="python_analytics",
    description=(
        "Pine library scripts run by the pine-runner over each universe symbol's daily bars "
        "→ features.stock_signal_pine_daily (buy / sell sessions only)."
    ),
)
def pine_signals(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_pine_signals()
    context.log.info("pine result=%s", {k: v for k, v in result.items() if k != "scripts"})
    return judged_materialization(context, result, _metadata(result), PINE_SPEC)


@asset(
    key=AssetKey(["engines", "candidate_outcome"]),
    check_specs=output_check_specs(AssetKey(["engines", "candidate_outcome"])),
    deps=[AssetKey(["engines", "scan"])],
    group_name="python_analytics",
    description=(
        "Settle proposed candidates against forward prices → research.candidate_outcome. "
        "Horizons that have not elapsed are skipped, not written as zero."
    ),
)
def candidate_outcome(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_candidate_outcome()
    context.log.info("candidate_outcome result=%s", result)
    return judged_materialization(context, result, _metadata(result), QUIET_ROWS_SPEC)


@asset(
    key=AssetKey(["engines", "suggestion_ledger"]),
    check_specs=output_check_specs(AssetKey(["engines", "suggestion_ledger"])),
    # engines/pine: the session's Pine signals are issued before settlement (S4).
    deps=[AssetKey(["engines", "volatility"]), AssetKey(["engines", "pine"]), *_MARKET],
    group_name="python_analytics",
    description=(
        "Suggestion ledger: issue the mechanical suggestions (SPY 30-delta put baseline, "
        "live simulator configs) and the Pine ones (pinned scripts' buy / sell signals) "
        "→ research.suggestion, then settle option suggestions with the simulator's walk "
        "→ research.suggestion_settlement. Append-only."
    ),
)
def suggestion_ledger(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_suggestion_ledger()
    context.log.info("suggestion_ledger result=%s", result)
    return judged_materialization(context, result, _metadata(result), SUGGESTION_SPEC)


@asset(
    key=AssetKey(["engines", "option_universe"]),
    check_specs=output_check_specs(AssetKey(["engines", "option_universe"])),
    deps=[_SEPA, *_MARKET],
    group_name="python_analytics",
    description=(
        "research.option_universe — the option universe as a rule: resident (watchlist, "
        "benchmarks), core (dollar-volume with hysteresis), edge (the stock screen's "
        "survivors). The Plugin reads it to decide what to enumerate."
    ),
)
def option_universe(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_option_universe()
    context.log.info("option_universe result=%s", result)
    return judged_materialization(context, result, _metadata(result), UNIVERSE_SPEC)


@asset(
    key=AssetKey(["engines", "option_pinned_contract"]),
    check_specs=output_check_specs(AssetKey(["engines", "option_pinned_contract"])),
    deps=[AssetKey(["engines", "option_universe"])],
    group_name="python_analytics",
    description=(
        "research.option_pinned_contract — the option contracts whose history the "
        "Plugin's retention window must not touch: held legs and anything traded in "
        "the last 180 days. Read from the Trade API over HTTP; a Trade API that is "
        "down skips the round rather than emptying the list."
    ),
)
def option_pinned_contract(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_option_pinned_contract()
    context.log.info("option_pinned_contract result=%s", result)
    return judged_materialization(context, result, _metadata(result), QUIET_ROWS_SPEC)


@asset(
    key=AssetKey(["engines", "signal_hit_fwd_fill"]),
    check_specs=output_check_specs(AssetKey(["engines", "signal_hit_fwd_fill"])),
    # signal_hit runs in research_trading_day after the lens sources (TD-156), so
    # this waits for them through it. _MARKET stays: before signal_hit joined the
    # batch this started at t=0, before the gate had judged the session whose bars
    # it reads, and beside the gate's doctor call (2026-09-29 02:30:21).
    deps=[AssetKey(["engines", "signal_hit"]), *_MARKET],
    group_name="python_analytics",
    description=(
        "Late-fill hit_5d / hit_20d on lens rows whose forward window has elapsed. "
        "The nightly signal_hit run only walks 3 days, so those columns were always NULL. "
        "Runs after husbandry_gate, like the other engines."
    ),
)
def signal_hit_fwd_fill(context: AssetExecutionContext) -> MaterializeResult:
    result = runners.run_signal_hit_fwd_fill()
    context.log.info("signal_hit_fwd_fill result=%s", result)
    return judged_materialization(context, result, _metadata(result), WINDOW_ROWS_SPEC)


ENGINE_ASSETS = [
    volatility,
    vrp,
    vrp_fwd_ret_20d,
    momentum,
    gex,
    surface,
    flow,
    terrain,
    forecast,
    event_radar,
    backtest,
    canonical_pnl,
    scan,
    pine_signals,
    candidate_outcome,
    suggestion_ledger,
    signal_hit_fwd_fill,
    option_universe,
    option_pinned_contract,
]
