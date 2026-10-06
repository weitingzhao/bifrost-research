"""Option position simulator HTTP routes — P2 (0.170.0).

Routes:
    POST /research/backtest/sim               run (and by default store) a simulation
    GET  /research/backtest/sim/{run_id}/detail   stored trades + equity curve

Synchronous: a run walks one symbol at a time and holds about a month of that
symbol's chain in memory (``ChainStore.load_windowed``), so the window length
does not set the memory; the symbols set the time. A parameter sweep belongs
in a Dagster job.

D10 BLOCKED — historical replay only. No execution path is touched.
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, model_validator

from bifrost_research.auth.deps import require_owner
from bifrost_research.db.conn import connect
from bifrost_research.engines.backtest.event_defs import check_entry_offset, default_entry_offset
from bifrost_research.engines.backtest.sim import STRUCTURES, SimConfig, run_sim
from bifrost_research.engines.backtest.sim.pine import PineRunnerUnavailable
from bifrost_research.repositories import backtest_run as repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/backtest", tags=["research-backtest"])

MAX_SYMBOLS = 10


class SimEntryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["earnings", "opex", "sepa_hit", "iv_percentile_threshold", "indicator_signal", "pine_signal"]
    params: dict[str, Any] = Field(default_factory=dict)


class SimStrikeAnchor(BaseModel):
    """Place the short strike against a Pine plot's level (P1, B5); see ``SimConfig``."""

    model_config = ConfigDict(extra="forbid")
    plot: str = Field(..., min_length=1, max_length=80)
    min_delta: float = Field(0.05, ge=0.0, lt=1.0)
    max_delta: float = Field(0.40, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _rails(self) -> "SimStrikeAnchor":
        if self.min_delta >= self.max_delta:
            raise ValueError("min_delta must be below max_delta")
        return self


class SimBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbols: list[str] = Field(..., min_length=1, max_length=MAX_SYMBOLS)
    start: date | None = None
    end: date | None = None
    structure: Literal[
        "short_put", "put_credit_spread", "call_credit_spread", "short_strangle", "iron_condor"
    ] = "short_put"
    target_dte: int = Field(45, ge=7, le=80)
    short_delta: float = Field(0.20, gt=0.0, lt=0.6)
    wing_width_pct: float = Field(0.05, gt=0.0, le=0.5)
    quantity: int = Field(1, ge=1, le=100)
    entry_every_sessions: int = Field(5, ge=1, le=60)
    # An event in place of the schedule: open ``entry_offset_sessions`` from each
    # one and let the simulator manage it (W2, 0.171.0). For a signal kind
    # (event_defs.SIGNAL_KINDS) 0 is the session after the signal and the
    # default; for earnings and OpEx the default stays -1, the session before.
    entry_event: SimEntryEvent | None = None
    entry_offset_sessions: int | None = Field(None, ge=-10, le=10)
    max_open_per_symbol: int = Field(3, ge=1, le=20)
    profit_take_pct: float | None = Field(0.5, gt=0.0, le=1.0)
    stop_loss_mult: float | None = Field(2.0, gt=0.0, le=20.0)
    dte_exit: int | None = Field(21, ge=0, le=80)
    max_stale_sessions: int | None = Field(3, ge=1, le=20)
    # An entry whose picked short delta is further than this from short_delta is
    # skipped (``delta_off_target``), not opened at the wrong strike. None turns it off.
    delta_tolerance: float | None = Field(0.05, gt=0.0, le=0.5)
    # P1 (pine_signal entries only): the script's own exit beside the premium
    # rules, and a strike placed against one of its plots. Both additive; the
    # response's summary then carries ``pine`` and ``pine_exit_comparison``.
    pine_exit: Literal["auto", "strategy", "reverse_plot"] | None = None
    strike_anchor: SimStrikeAnchor | None = None
    price_field: Literal["vwap", "close"] = "vwap"
    slippage_scale: float = Field(1.0, ge=0.0, le=10.0)
    commission_per_contract: float = Field(0.65, ge=0.0, le=10.0)
    capital: float = Field(100_000.0, gt=0.0)
    persist: bool = True
    persist_trades: bool = True
    hypothesis_id: str | None = None

    @model_validator(mode="after")
    def _window(self) -> "SimBody":
        end = self.end or date.today()
        start = self.start or (end - timedelta(days=730))
        if start >= end:
            raise ValueError("start must be before end")
        if (end - start).days > 366 * 5:
            raise ValueError("window longer than five years")
        self.start, self.end = start, end
        kind = self.entry_event.kind if self.entry_event else None
        if self.entry_offset_sessions is None:
            self.entry_offset_sessions = default_entry_offset(kind)
        check_entry_offset(kind, self.entry_offset_sessions)
        if (self.pine_exit or self.strike_anchor) and (not self.entry_event or self.entry_event.kind != "pine_signal"):
            raise ValueError("pine_exit and strike_anchor need entry_event kind pine_signal")
        return self


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


@router.post("/sim", dependencies=[Depends(require_owner)])
def simulate(body: SimBody) -> dict[str, Any]:
    assert body.start is not None and body.end is not None
    cfg = SimConfig(
        structure=body.structure,
        target_dte=body.target_dte,
        short_delta=body.short_delta,
        wing_width_pct=body.wing_width_pct,
        quantity=body.quantity,
        entry_every_sessions=body.entry_every_sessions,
        entry_event=body.entry_event.model_dump() if body.entry_event else None,
        entry_offset_sessions=int(body.entry_offset_sessions if body.entry_offset_sessions is not None else -1),
        max_open_per_symbol=body.max_open_per_symbol,
        profit_take_pct=body.profit_take_pct,
        stop_loss_mult=body.stop_loss_mult,
        dte_exit=body.dte_exit,
        max_stale_sessions=body.max_stale_sessions,
        delta_tolerance=body.delta_tolerance,
        pine_exit=body.pine_exit,
        strike_anchor=body.strike_anchor.model_dump() if body.strike_anchor else None,
        price_field=body.price_field,
        slippage_scale=body.slippage_scale,
        commission_per_contract=body.commission_per_contract,
        capital=body.capital,
    )
    conn = _connect_or_503()
    try:
        try:
            result = run_sim(conn, body.symbols, body.start, body.end, cfg)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PineRunnerUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        params = {
            **result.params,
            "symbols": [s.strip().upper() for s in body.symbols],
            "start": body.start.isoformat(),
            "end": body.end.isoformat(),
        }
        run: dict[str, Any] = {"id": None, "persisted": False}
        if body.persist:
            try:
                run = repo.create_sim_run(
                    conn,
                    params=params,
                    summary=result.summary,
                    trades=result.trades,
                    equity=result.equity,
                    lookback_years=max(1, math.ceil((body.end - body.start).days / 365)),
                    persist_trades=body.persist_trades,
                    hypothesis_id=body.hypothesis_id,
                )
            except Exception as exc:  # noqa: BLE001 — e.g. the 0.170.0 DDL not applied yet
                logger.exception("sim run persist failed; returning it unpersisted")
                run = {"id": None, "persisted": False, "error": str(exc)[:300]}
            if body.hypothesis_id and run.get("id"):
                try:
                    repo.append_to_hypothesis(conn, body.hypothesis_id, run["id"])
                except Exception:  # noqa: BLE001
                    logger.exception("append_to_hypothesis failed for %s", body.hypothesis_id)
        return {
            "ok": True,
            "data": {
                "run_id": run.get("id"),
                "run": run,
                "summary": result.summary,
                "trades": result.trades,
                "equity": result.equity,
                "params": params,
                "advisory": "D10 BLOCKED — historical replay only",
            },
        }
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


@router.get("/sim/structures", dependencies=[Depends(require_owner)])
def structures() -> dict[str, Any]:
    return {
        "ok": True,
        "data": {name: [leg.label for leg in legs] for name, legs in STRUCTURES.items()},
    }


@router.get("/sim/{run_id}/detail", dependencies=[Depends(require_owner)])
def sim_detail(run_id: str = Path(..., min_length=1, max_length=64)) -> dict[str, Any]:
    conn = _connect_or_503()
    try:
        row = repo.get_run(conn, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"backtest_run {run_id} not found")
        detail = repo.get_sim_detail(conn, run_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("sim detail failed")
        raise HTTPException(status_code=503, detail=str(exc)[:300]) from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "data": {"row": row, **detail}}
