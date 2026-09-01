"""Post-approve validate hooks — Wave LO-3.

Runs stock-leg event backtests for promoted hypotheses and attaches evidence
drafts.  Option templates remain gated until LO-5 data program completes.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from bifrost_research.engines.backtest.event_defs import EventDef
from bifrost_research.engines.backtest.event_query import run_event_query
from bifrost_research.repositories import ai_action_log as action_repo
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import backtest_run as bt_repo
from bifrost_research.repositories import hypothesis as hyp_repo

logger = logging.getLogger(__name__)

STOCK_TEMPLATE = "long_stock_event"
OPTION_TEMPLATES = frozenset({"long_atm_straddle", "short_strangle_30d"})


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...


def _option_coverage_available(conn: _Connection) -> bool:
    """True when ``raw_market.option_daily`` spans at least 90 days."""
    sql = """
        SELECT MIN(trade_date), MAX(trade_date), COUNT(*)
        FROM raw_market.option_daily
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        if not row or row[2] is None or int(row[2]) < 100:
            return False
        if row[0] is None or row[1] is None:
            return False
        span = (row[1] - row[0]).days
        return span >= 90
    except Exception:
        return False


def validate_hypothesis_stock_leg(
    conn: _Connection,
    *,
    hypothesis_id: str,
    symbol: str | None = None,
    lookback_years: int = 3,
) -> dict[str, Any]:
    """Run earnings stock-leg backtest and attach evidence + eod_verdict draft."""
    hyp = hyp_repo.get_hypothesis(conn, hypothesis_id)
    if hyp is None:
        return {"ok": False, "error": "hypothesis not found"}

    sym = (symbol or (hyp.get("symbols") or [None])[0] or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "no symbol for hypothesis"}

    try:
        bt = run_event_query(
            EventDef(kind="earnings", params={}),
            STOCK_TEMPLATE,
            lookback_years=lookback_years,
            conn=conn,
        )
    except Exception as exc:
        logger.warning("validate backtest failed hyp=%s: %s", hypothesis_id, exc)
        return {"ok": False, "error": str(exc)}

    summary = bt.get("summary") or {}
    win_rate = summary.get("win_rate")

    backtest_run_id: str | None = None
    try:
        row = bt_repo.create_run(
            conn,
            event_def={"kind": "earnings", "params": {}},
            strategy_template=STOCK_TEMPLATE,
            fill_config={
                "slippage_pct_of_spread": 0.2,
                "commission_per_contract": 0.65,
                "multiplier": 100,
                "exercise_style": "american_no_early",
            },
            lookback_years=lookback_years,
            summary=summary,
            walk_forward=None,
            benchmark=None,
            hypothesis_id=hypothesis_id,
        )
        backtest_run_id = str(row.get("id") or "")
    except Exception as exc:
        logger.warning("persist backtest_run failed: %s", exc)

    if backtest_run_id:
        linked = list(hyp.get("linked_backtest_ids") or [])
        if backtest_run_id not in linked:
            linked.append(backtest_run_id)
            hyp_repo.patch_hypothesis(conn, hypothesis_id, {"linked_backtest_ids": linked})

    proposed = "validated" if isinstance(win_rate, (int, float)) and win_rate >= 0.5 else "rejected"
    rationale = (
        f"Auto stock-leg backtest ({STOCK_TEMPLATE}) win_rate={win_rate}; "
        f"events={summary.get('event_count', 'n/a')}"
    )

    action = action_repo.insert_action(
        conn,
        action_kind="validate_hook_eod_verdict",
        action_source="loop_validate",
        input_payload={"hypothesis_id": hypothesis_id, "symbol": sym},
        output_payload={"summary": summary, "backtest_run_id": backtest_run_id},
        status="proposed",
    )
    draft = draft_repo.insert_draft(
        conn,
        kind="eod_verdict",
        payload={
            "hypothesis_id": hypothesis_id,
            "proposed_status": proposed,
            "rationale": rationale,
            "backtest_run_id": backtest_run_id,
            "template": STOCK_TEMPLATE,
            "auto_validate": True,
        },
        scope=f"hypothesis:{hypothesis_id}",
        generated_by="loop_validate",
        linked_action_id=action["id"],
    )

    return {
        "ok": True,
        "hypothesis_id": hypothesis_id,
        "backtest_run_id": backtest_run_id,
        "eod_verdict_draft_id": draft["id"],
        "proposed_status": proposed,
        "summary": summary,
    }


def run_validate_hooks_for_run(
    conn: _Connection,
    *,
    run_id: str,
    hypothesis_ids: list[str],
    auto_validate: bool = True,
) -> dict[str, Any]:
    """Validate each hypothesis promoted during a harness run."""
    if not auto_validate or not hypothesis_ids:
        return {"validated": [], "skipped": len(hypothesis_ids)}

    results: list[dict[str, Any]] = []
    for hid in hypothesis_ids:
        hyp = hyp_repo.get_hypothesis(conn, hid)
        sym = (hyp.get("symbols") or [None])[0] if hyp else None
        results.append(validate_hypothesis_stock_leg(conn, hypothesis_id=hid, symbol=sym))

    return {"validated": results, "option_coverage": _option_coverage_available(conn)}


__all__ = [
    "OPTION_TEMPLATES",
    "STOCK_TEMPLATE",
    "run_validate_hooks_for_run",
    "validate_hypothesis_stock_leg",
]
