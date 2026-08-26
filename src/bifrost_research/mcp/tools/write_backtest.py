"""MCP write tool: research.backtest.run_event_query (Wave RS-E4.1).

dry_run=true → planned params preview (no compute / no DB write).
dry_run=false → run event query + persist backtest_run (requires approval_token).
D10 BLOCKED — historical replay only.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.engines.backtest.event_defs import EventDef
from bifrost_research.engines.backtest.event_query import run_event_query
from bifrost_research.engines.backtest.fills import FillConfig
from bifrost_research.engines.backtest.strategy_templates import TEMPLATES
from bifrost_research.mcp.tools._common import with_conn
from bifrost_research.mcp.tools._write_common import (
    WRITE_SUFFIX,
    diff_preview,
    executed_ok,
    require_approval_or_error,
    safe_err,
)
from bifrost_research.repositories import backtest_run as repo


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.backtest.run_event_query",
        description=(
            "Run an event-driven backtest (research.backtest_run). "
            "dry_run returns the planned params only. "
            f"{WRITE_SUFFIX}"
        ),
    )
    def run_event_query_tool(
        strategy_template: str,
        event_kind: str = "earnings",
        event_params: dict[str, Any] | None = None,
        lookback_years: int = 3,
        hypothesis_id: str | None = None,
        fill_config: dict[str, Any] | None = None,
        template_kwargs: dict[str, Any] | None = None,
        dry_run: bool = True,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "strategy_template": strategy_template,
            "event_kind": event_kind,
            "event_params": event_params or {},
            "lookback_years": lookback_years,
            "hypothesis_id": hypothesis_id,
            "fill_config": fill_config,
            "template_kwargs": template_kwargs or {},
        }
        gate = require_approval_or_error(
            dry_run=dry_run,
            approval_token=approval_token,
            tool="research.backtest.run_event_query",
            arguments=args,
        )
        if gate is not None:
            return gate

        preview = {
            "strategy_template": strategy_template,
            "event_def": {"kind": event_kind, "params": event_params or {}},
            "lookback_years": lookback_years,
            "hypothesis_id": hypothesis_id,
            "fill_config": fill_config,
            "template_kwargs": template_kwargs or {},
        }
        impact = {
            "creates_row": True,
            "table": "research.backtest_run",
            "may_link_hypothesis": bool(hypothesis_id),
            "mutates": ["INSERT", "UPDATE(hypothesis.linked_backtest_ids)"],
            "compute": "run_event_query",
        }

        if dry_run:
            known = strategy_template in TEMPLATES
            preview["template_known"] = known
            preview["available_templates"] = sorted(TEMPLATES)
            return diff_preview(
                diff_kind="run_backtest",
                preview=preview,
                impact=impact,
                dry_run=True,
            )

        if strategy_template not in TEMPLATES:
            return safe_err(
                f"unknown strategy_template {strategy_template!r}; "
                f"available: {sorted(TEMPLATES)}"
            )

        def _run(conn: Any) -> dict[str, Any]:
            event_def = EventDef(kind=event_kind, params=event_params or {})  # type: ignore[arg-type]
            fill_cfg = None
            if fill_config:
                fill_cfg = FillConfig(
                    slippage_pct_of_spread=float(
                        fill_config.get("slippage_pct_of_spread", 0.2)
                    ),
                    commission_per_contract=float(
                        fill_config.get("commission_per_contract", 0.65)
                    ),
                    multiplier=int(fill_config.get("multiplier", 100)),
                    exercise_style=fill_config.get(  # type: ignore[arg-type]
                        "exercise_style", "american_no_early"
                    ),
                )
            try:
                result = run_event_query(
                    event_def,
                    template_name=strategy_template,
                    lookback_years=int(lookback_years),
                    conn=conn,
                    fill_config=fill_cfg,
                    **(template_kwargs or {}),
                )
            except NotImplementedError as exc:
                return safe_err(str(exc))
            except ValueError as exc:
                return safe_err(str(exc))

            fill_dump = fill_config or {
                "slippage_pct_of_spread": 0.2,
                "commission_per_contract": 0.65,
                "multiplier": 100,
                "exercise_style": "american_no_early",
            }
            try:
                row = repo.create_run(
                    conn,
                    event_def=event_def.to_dict(),
                    strategy_template=strategy_template,
                    fill_config=fill_dump,
                    lookback_years=int(lookback_years),
                    summary=result.get("summary", {}),
                    walk_forward=None,
                    benchmark=None,
                    hypothesis_id=hypothesis_id,
                )
            except Exception as exc:  # noqa: BLE001
                return executed_ok(
                    "run_backtest",
                    {
                        "summary": result.get("summary"),
                        "persisted": False,
                        "error": str(exc),
                        "runs_count": len(result.get("runs") or []),
                    },
                )

            if hypothesis_id and row.get("id"):
                try:
                    repo.append_to_hypothesis(conn, hypothesis_id, row["id"])
                except Exception:  # noqa: BLE001
                    pass

            return executed_ok(
                "run_backtest",
                {
                    "run_id": row.get("id"),
                    "run": row,
                    "summary": result.get("summary"),
                    "runs_count": len(result.get("runs") or []),
                    "event_source": result.get("event_source"),
                },
            )

        return with_conn(_run)
