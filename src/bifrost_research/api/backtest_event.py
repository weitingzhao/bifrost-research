"""Event-driven backtest HTTP routes — Wave RS-C4.

Response envelope: ``{"ok": bool, "data": ..., "error"?: str}``.

Routes:
    POST /research/backtest/event-query
    GET  /research/backtest/runs?hypothesis_id=&limit=&offset=
    GET  /research/backtest/run/{run_id}

D10 BLOCKED — historical replay only. No execution path is touched.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from bifrost_research.db.conn import connect
from bifrost_research.engines.backtest.benchmark import (
    spy_buy_hold_metrics,
    zero_signal_control,
)
from bifrost_research.engines.backtest.event_defs import EventDef
from bifrost_research.engines.backtest.event_query import run_event_query
from bifrost_research.engines.backtest.fills import FillConfig
from bifrost_research.engines.backtest.strategy_templates import TEMPLATES
from bifrost_research.repositories import backtest_run as repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/backtest", tags=["research-backtest"])


class EventDefModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["earnings", "opex", "sepa_hit", "iv_percentile_threshold", "sql"]
    params: dict[str, Any] = Field(default_factory=dict)


class FillConfigModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    slippage_pct_of_spread: float = 0.2
    commission_per_contract: float = 0.65
    multiplier: int = 100
    exercise_style: Literal["american_no_early", "european"] = "american_no_early"


class EventQueryBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    event_def: EventDefModel
    strategy_template: str = Field(..., min_length=1, max_length=80)
    fill_config: FillConfigModel | None = None
    lookback_years: int = Field(3, ge=1, le=10)
    hypothesis_id: str | None = None
    include_walk_forward: bool = False
    include_benchmark: bool = False
    template_kwargs: dict[str, Any] = Field(default_factory=dict)


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


def _validate_template(name: str) -> None:
    if name not in TEMPLATES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown strategy_template {name!r}; available: {sorted(TEMPLATES)}"
            ),
        )


@router.post("/event-query")
def event_query(body: EventQueryBody) -> dict[str, Any]:
    _validate_template(body.strategy_template)
    event_def = EventDef(kind=body.event_def.kind, params=body.event_def.params or {})
    fill_cfg = (
        FillConfig(
            slippage_pct_of_spread=body.fill_config.slippage_pct_of_spread,
            commission_per_contract=body.fill_config.commission_per_contract,
            multiplier=body.fill_config.multiplier,
            exercise_style=body.fill_config.exercise_style,
        )
        if body.fill_config is not None
        else None
    )
    conn = _connect_or_503()
    try:
        try:
            result = run_event_query(
                event_def,
                template_name=body.strategy_template,
                lookback_years=body.lookback_years,
                conn=conn,
                fill_config=fill_cfg,
                **(body.template_kwargs or {}),
            )
        except NotImplementedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        walk_forward_payload: list[dict[str, Any]] | None = None
        benchmark_payload: dict[str, Any] | None = None
        if body.include_walk_forward or body.include_benchmark:
            # Build a coarse price series from the run entries for a
            # walk-forward / benchmark proxy. This intentionally uses the
            # per-event exit P&L as a return proxy — good enough for a v1
            # sanity check; real WF over the underlying is future work.
            proxy_series = [
                (
                    _parse_date(run.get("exit_ts") or run.get("event_date")),
                    100.0 + float(run.get("pnl", 0.0)),
                )
                for run in result.get("runs", [])
                if run.get("exit_ts") or run.get("event_date")
            ]
            proxy_series = [(d, p) for d, p in proxy_series if d is not None]
            if body.include_walk_forward and proxy_series:
                from bifrost_research.engines.backtest.walk_forward import (
                    aggregate_oos,
                    run_walk_forward,
                )

                wf = run_walk_forward(
                    strategy_fn=None,
                    price_series=proxy_series,
                    window_years=1,
                    oos_months=3,
                )
                walk_forward_payload = {"windows": wf, "aggregate": aggregate_oos(wf)}
            if body.include_benchmark and proxy_series:
                spy = spy_buy_hold_metrics(proxy_series)
                control = zero_signal_control(
                    strategy_fn=None,
                    price_series=proxy_series,
                    window_years=1,
                    oos_months=3,
                )
                benchmark_payload = {"spy_buy_hold": spy, "zero_signal_control": control}

        fill_dump = (
            body.fill_config.model_dump() if body.fill_config is not None else FillConfigModel().model_dump()
        )
        try:
            row = repo.create_run(
                conn,
                event_def=event_def.to_dict(),
                strategy_template=body.strategy_template,
                fill_config=fill_dump,
                lookback_years=body.lookback_years,
                summary=result.get("summary", {}),
                walk_forward=walk_forward_payload,
                benchmark=benchmark_payload,
                hypothesis_id=body.hypothesis_id,
            )
        except Exception as exc:
            logger.exception("backtest_run create failed; returning run without persistence")
            row = {"id": None, "persisted": False, "error": str(exc)}

        if body.hypothesis_id and row.get("id"):
            try:
                repo.append_to_hypothesis(conn, body.hypothesis_id, row["id"])
            except Exception:
                logger.exception("append_to_hypothesis failed for %s", body.hypothesis_id)

        payload = {
            "run_id": row.get("id"),
            "run": row,
            "summary": result.get("summary", {}),
            "runs": result.get("runs", []),
            "event_source": result.get("event_source"),
            "event_source_notes": result.get("event_source_notes"),
            "skipped_events": result.get("skipped_events", 0),
            "walk_forward": walk_forward_payload,
            "benchmark": benchmark_payload,
            "advisory": "D10 BLOCKED — historical replay only",
        }
        return _ok(payload)
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("/runs")
def list_runs(
    hypothesis_id: str | None = Query(None),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        rows = repo.list_runs(conn, hypothesis_id=hypothesis_id, limit=limit, offset=offset)
    except Exception as exc:
        logger.exception("backtest/runs failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return _ok(
        {
            "rows": rows,
            "count": len(rows),
            "limit": limit,
            "offset": offset,
            "hypothesis_id": hypothesis_id,
        }
    )


@router.get("/run/{run_id}")
def get_run(run_id: str = Path(..., min_length=1, max_length=64)) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        row = repo.get_run(conn, run_id)
    except Exception as exc:
        logger.exception("backtest/run get failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if row is None:
        raise HTTPException(status_code=404, detail=f"backtest_run {run_id} not found")
    return _ok({"row": row})


def _parse_date(value: Any):
    """Parse an ISO date string; return None on failure."""
    if value is None:
        return None
    from datetime import date, datetime

    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except Exception:
        return None


__all__ = ["router", "EventQueryBody", "EventDefModel", "FillConfigModel"]
