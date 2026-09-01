"""Thin runners that wrap scheduler / engine entrypoints for Dagster assets.

D10 BLOCKED — advisory writes to features_daily.* / research.* only.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from bifrost_research.scheduler import engines as engine_sched
from bifrost_research.scheduler import volatility as vol_sched

logger = logging.getLogger(__name__)


def run_volatility(*, lookback_days: int = 3) -> dict[str, Any]:
    """Run all volatility Cron slots (max-pain, atm-iv-pcr, iv-percentile)."""
    results = []
    for slot in vol_sched.SLOT_NAMES:
        results.append(vol_sched.run_slot(slot, lookback_days=lookback_days))
    return {"engine": "volatility", "slots": results}


def run_momentum(*, lookback_days: int = 2) -> dict[str, Any]:
    return engine_sched.run_slot("momentum", lookback_days=lookback_days)


def run_gex(*, lookback_days: int = 2) -> dict[str, Any]:
    return engine_sched.run_slot("gex", lookback_days=lookback_days)


def run_surface(*, lookback_days: int = 2) -> dict[str, Any]:
    return engine_sched.run_slot("iv-surface", lookback_days=lookback_days)


def run_flow(*, lookback_days: int = 2) -> dict[str, Any]:
    return engine_sched.run_slot("flow", lookback_days=lookback_days)


def run_terrain(*, lookback_days: int = 2) -> dict[str, Any]:
    return engine_sched.run_slot("terrain", lookback_days=lookback_days)


def run_forecast(*, lookback_days: int = 2) -> dict[str, Any]:
    return engine_sched.run_slot("forecast", lookback_days=lookback_days)


def run_event_radar(
    *,
    sample_text: str | None = None,
    input_dir: str | None = None,
) -> dict[str, Any]:
    """Run Event Radar from Research-workspace input files (decision A).

    Prefer ``EVENT_RADAR_INPUT_DIR`` / Cron file ingest. ``sample_text`` remains
    a fallback for Dagster dry materialization when the input directory is empty.
    """
    from bifrost_research.engines.event_radar.ingest import ingest_directory
    from bifrost_research.engines.event_radar.pipeline import run_pipeline, upsert_events

    summary = ingest_directory(input_dir, upsert=True, archive=None)
    if summary.files_processed or summary.files_seen:
        return {
            "engine": "event_radar",
            "mode": "file_ingest",
            **summary.to_dict(),
        }

    payload = sample_text or (
        "Fed officials signal rate pause. Tech mega-caps rally on AI spend. "
        "Oil inventories draw unexpectedly."
    )
    result = run_pipeline(payload, source="dagster-fallback")
    written = 0
    try:
        from bifrost_research.db.conn import connect

        conn = connect()
        try:
            written = upsert_events(conn, result)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — tolerate missing DB in local dry runs
        logger.warning("event_radar upsert skipped: %s", exc)
    return {
        "engine": "event_radar",
        "mode": "sample_fallback",
        "batch_id": result.batch_id,
        "raw_count": result.raw_count,
        "kept": len(result.kept),
        "dropped": len(result.dropped),
        "self_check": result.self_check,
        "rows_written": written,
        "advisory": "D10 BLOCKED — event radar is advisory only",
    }


def run_backtest(*, as_of: date | None = None) -> dict[str, Any]:
    """Settlement / accuracy scaffold — aggregates recent forecast settlements.

    Full vectorbt strategy backtests land in a later wave; here we exercise the
    settlement helpers against empty/partial data safely.
    """
    from bifrost_research.engines.backtest.settlement import aggregate_accuracy

    _ = as_of
    summary = aggregate_accuracy([])
    return {
        "engine": "backtest",
        "scaffolding": True,
        "sessions_settled": summary.sessions_settled,
        "path_hit_rate": summary.path_hit_rate,
        "message": (
            "Backtest asset scaffold — provide ForecastSettlement rows to "
            "aggregate_accuracy for real metrics. D10 BLOCKED."
        ),
        "advisory": "D10 BLOCKED",
    }


def run_canonical_pnl(
    *,
    lookback_months: int = 6,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Canonical structure PnL cohort (Dagster stub / CronJob shared runner)."""
    from bifrost_research.db.calendar import (
        load_symbols_from_env_or_query,
        union_iv_radar_benchmarks,
    )
    from bifrost_research.db.conn import connect
    from bifrost_research.engines.canonical_pnl import run_cohort

    conn = connect()
    try:
        underlyings = load_symbols_from_env_or_query(conn)
        universe = union_iv_radar_benchmarks(underlyings)
        result = run_cohort(
            conn,
            symbols=universe,
            lookback_months=lookback_months,
            dry_run=dry_run,
        )
        result["engine"] = "canonical_pnl"
        result["advisory"] = "D10 BLOCKED"
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_scan(*, lookback_days: int = 3) -> dict[str, Any]:
    """Materialized scanner — depends on upstream CronJobs (vrp/opex) finishing first."""
    from bifrost_research.engines.scan.entry import run

    result = run(lookback_days=lookback_days)
    result["engine"] = "scan"
    result["advisory"] = "D10 BLOCKED"
    return result


def run_candidate_outcome(*, lookback_days: int = 90) -> dict[str, Any]:
    """Settle what happened to candidates the Loop proposed.

    Runs after `scan` so a candidate produced today is picked up on the next
    materialization, once its forward window has actually elapsed.
    """
    from bifrost_research.engines.candidate_outcome.entry import run

    result = run(lookback_days=lookback_days)
    result["engine"] = "candidate_outcome"
    result["advisory"] = "D10 BLOCKED"
    return result
