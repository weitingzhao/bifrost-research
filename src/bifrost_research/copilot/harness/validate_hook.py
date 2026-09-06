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
from bifrost_research.repositories import objective as obj_repo

logger = logging.getLogger(__name__)

STOCK_TEMPLATE = "long_stock_event"
OPTION_TEMPLATES = frozenset({"long_atm_straddle", "short_strangle_30d"})
# B4: a hypothesis born of an option-lens objective (IV hot → premium is rich)
# is a short-premium thesis; the stock leg is the wrong instrument to test it.
OPTION_TEMPLATE = "short_strangle_30d"
OPTION_TAGS = frozenset({"option", "options", "iv_hot", "premium", "vol"})


def instrument_for(hyp: dict[str, Any], *, objective_policy: dict[str, Any] | None = None) -> str:
    """``"option"`` when the thesis is about premium, else ``"stock"``.

    Read off the tags first, then the objective the hypothesis came from: the
    legacy option scan (``universe_mode: scan_legacy``) proposes on IV lenses,
    the stock modes on structure.
    """
    tags = {str(t).strip().lower() for t in (hyp.get("tags") or [])}
    if tags & OPTION_TAGS:
        return "option"
    policy = objective_policy or {}
    mode = str(policy.get("universe_mode") or "").strip()
    if mode == "scan_legacy":
        return "option"
    return "stock"


def template_for(instrument: str, *, option_coverage: bool) -> str:
    """The backtest template for the instrument — the option leg only when the
    option history is deep enough to mean anything (LO-5 gate)."""
    if instrument == "option" and option_coverage:
        return OPTION_TEMPLATE
    return STOCK_TEMPLATE


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
    objective_policy: dict[str, Any] | None = None,
    option_coverage: bool | None = None,
) -> dict[str, Any]:
    """Run the earnings backtest for the hypothesis' instrument and attach the
    evidence + an eod_verdict draft.

    Named for the stock leg it always ran; since B4 it picks the template by
    instrument (``instrument_for`` / ``template_for``) and falls back to the
    stock leg whenever the option history cannot carry a verdict.
    """
    hyp = hyp_repo.get_hypothesis(conn, hypothesis_id)
    if hyp is None:
        return {"ok": False, "error": "hypothesis not found"}

    sym = (symbol or (hyp.get("symbols") or [None])[0] or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "no symbol for hypothesis"}

    instrument = instrument_for(hyp, objective_policy=objective_policy)
    coverage = _option_coverage_available(conn) if option_coverage is None else option_coverage
    template = template_for(instrument, option_coverage=coverage)

    # The symbol was resolved above and then never used: the query ran with
    # empty params, so every hypothesis was validated against the same
    # market-wide aggregate. Measured on the live warehouse — market-wide
    # win_rate 0.3243 rejects everything, while WT's own record is 0.6 over 5
    # events and SCCO's is 0.5 over 4. The bug did not just mislabel the
    # evidence, it inverted the verdict, and it cost 29s a call instead of 3.
    event_params = {"symbols": [sym]}
    try:
        bt = run_event_query(
            EventDef(kind="earnings", params=event_params),
            template,
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
            event_def={"kind": "earnings", "params": dict(event_params)},
            strategy_template=template,
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
    # `event_count` has never been a key on the summary — it is `n_events` — so
    # every verdict has read "events=n/a" and hidden its own sample size. On this
    # data a symbol's record is four or five earnings, which is thin enough that
    # the reader has to see it next to the verdict.
    n_events = summary.get("n_events")
    leg = "option-leg" if template != STOCK_TEMPLATE else "stock-leg"
    rationale = (
        f"Auto {leg} backtest ({template}) for {sym}: "
        f"win_rate={win_rate} over {n_events if n_events is not None else 'n/a'} earnings "
        f"in {lookback_years}y"
        + (
            f" — thesis is about {instrument} premium but the option history is too thin, "
            "so the stock leg stands in"
            if instrument == "option" and template == STOCK_TEMPLATE
            else ""
        )
    )

    action = action_repo.insert_action(
        conn,
        action_kind="validate_hook_eod_verdict",
        action_source="loop_validate",
        input_payload={
            "hypothesis_id": hypothesis_id,
            "symbol": sym,
            "instrument": instrument,
            "template": template,
        },
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
            "template": template,
            "instrument": instrument,
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
        "instrument": instrument,
        "template": template,
        "summary": summary,
    }


def _objective_policy_for_run(conn: _Connection, run_id: str) -> dict[str, Any] | None:
    """The policy of the objective this run belongs to — fail-soft."""
    try:
        run = obj_repo.get_run(conn, run_id) or {}
        objective_id = str(run.get("objective_id") or "").strip()
        if not objective_id:
            return None
        objective = obj_repo.get_objective(conn, objective_id) or {}
        policy = objective.get("policy_json")
        return policy if isinstance(policy, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.info("validate hooks: objective policy unavailable for %s: %s", run_id, str(exc)[:120])
        return None


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

    policy = _objective_policy_for_run(conn, run_id)
    coverage = _option_coverage_available(conn)
    results: list[dict[str, Any]] = []
    for hid in hypothesis_ids:
        hyp = hyp_repo.get_hypothesis(conn, hid)
        sym = (hyp.get("symbols") or [None])[0] if hyp else None
        results.append(
            validate_hypothesis_stock_leg(
                conn,
                hypothesis_id=hid,
                symbol=sym,
                objective_policy=policy,
                option_coverage=coverage,
            )
        )

    return {"validated": results, "option_coverage": coverage}


__all__ = [
    "OPTION_TEMPLATE",
    "OPTION_TEMPLATES",
    "STOCK_TEMPLATE",
    "instrument_for",
    "run_validate_hooks_for_run",
    "template_for",
    "validate_hypothesis_stock_leg",
]
