"""Dagster asset: dw_stock.mart_sepa_feature_daily → features.stock_signal_sepa_daily."""

from typing import Any

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    AssetExecutionContext,
    AssetKey,
    MaterializeResult,
    asset,
    asset_check,
)

from bifrost_research.orchestration.sepa_projection import (
    ACCEPTED_GAPS,
    COVERAGE_SESSIONS,
    missing_sessions,
    newest_session,
    off_session_dates,
    run_sepa_projection,
)

# Upstreams: husbandry_gate (market_eod + flex enqueues) must pass, and dbt must
# have rebuilt the mart this asset projects. Without the mart edge the projection
# only followed dbt by luck: on 2026-09-29 (run e1dd41e5) the mart landed at
# 02:33:29 and the projection read it at 02:36:21, with nothing ordering the two.
# dbt is one step, so this waits for the whole dbt build and is skipped if it fails.
_GATE = AssetKey(["batch", "husbandry_gate"])
_FEATURE_MART = AssetKey(["mart_sepa_feature_daily"])


def _metadata(result: dict[str, Any]) -> dict[str, Any]:
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
    key=AssetKey(["features", "sepa_projection"]),
    deps=[_GATE, _FEATURE_MART],
    group_name="feature_store",
    description=(
        "Project dbt mart_sepa_feature_daily → features.stock_signal_sepa_daily. "
        "Runs after husbandry_gate (Market EOD + Flex outcome) and after the dbt "
        "build that rebuilds the mart; a failed dbt build skips it."
    ),
)
def sepa_projection(context: AssetExecutionContext) -> MaterializeResult:
    from bifrost_research.db.conn import connect

    from bifrost_research.db.calendar import latest_closed_session

    conn = connect()
    try:
        # The New York session, written explicitly: the database's current_date is
        # UTC and this runs at 02:3x UTC, one calendar day after the session (TD-87).
        session = latest_closed_session(conn)
        result = run_sepa_projection(conn, trade_date=session)
        context.log.info("sepa_projection result=%s", result)
        if result.get("skipped") and result.get("reason"):
            context.log.warning("sepa_projection skipped: %s", result.get("reason"))
        return MaterializeResult(metadata=_metadata(result))
    finally:
        try:
            conn.close()
        except Exception:
            pass


@asset_check(
    asset=sepa_projection,
    name="sessions_are_trading_days",
    blocking=False,
    description=(
        "TD-87 ratchet: features.stock_signal_sepa_daily holds no weekend, NYSE-holiday "
        "or future trade_date, and its newest trade_date is the latest closed New York session."
    ),
)
def sepa_sessions_are_trading_days() -> AssetCheckResult:
    from bifrost_research.db.calendar import latest_closed_session
    from bifrost_research.db.conn import connect

    conn = connect()
    try:
        off = off_session_dates(conn)
        newest = newest_session(conn)
        session = latest_closed_session(conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return AssetCheckResult(
        passed=not off and newest == session,
        severity=AssetCheckSeverity.ERROR,
        metadata={
            "off_session_dates": len(off),
            "off_session_sample": ", ".join(d.isoformat() for d in off[:20]) or "none",
            "newest_trade_date": newest.isoformat() if newest else "none",
            "new_york_session": session.isoformat(),
        },
    )


@asset_check(
    asset=sepa_projection,
    name="sepa_covers_recent_sessions",
    blocking=False,
    description=(
        f"TD-189 ratchet: every NYSE session among the last {COVERAGE_SESSIONS} closed "
        "sessions has SEPA rows. The projection writes one session a night and never "
        "catches up, so a night the batch misses is lost unless research_trading_day "
        "is re-run before the next close. WARN on any gap not in ACCEPTED_GAPS."
    ),
)
def sepa_covers_recent_sessions() -> AssetCheckResult:
    from bifrost_research.db.calendar import latest_closed_session
    from bifrost_research.db.conn import connect

    conn = connect()
    try:
        session = latest_closed_session(conn)
        missing = missing_sessions(conn, newest=session)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    new_gaps = [d for d in missing if d not in ACCEPTED_GAPS]
    accepted = [d for d in missing if d in ACCEPTED_GAPS]
    return AssetCheckResult(
        passed=not new_gaps,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "sessions_checked": COVERAGE_SESSIONS,
            "newest_session": session.isoformat(),
            "missing_sessions": len(new_gaps),
            "missing_sample": ", ".join(d.isoformat() for d in new_gaps[:20]) or "none",
            "accepted_gaps": ", ".join(d.isoformat() for d in accepted) or "none",
        },
    )


SEPA_PROJECTION_ASSETS = [sepa_projection]
SEPA_PROJECTION_CHECKS = [sepa_sessions_are_trading_days, sepa_covers_recent_sessions]
