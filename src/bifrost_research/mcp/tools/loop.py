"""MCP read tools: research.loop.* — research-loop-automation D1.

  research.loop.list_runs          — recent harness runs, one line each
  research.loop.get_run            — one run digested: plan · funnel · judges per model · evidence · report
  research.loop.explain_candidate  — one candidate of a run: why it was proposed, what would unmake it
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.copilot.harness import run_digest
from bifrost_research.mcp.tools._common import READ_ONLY_SUFFIX, err, ok, with_conn

MAX_LIST = 100


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="research.loop.list_runs",
        description=(
            "Recent harness runs of the Research Loop, one line each: status, objective, "
            "candidate / draft counts, plan provenance (LLM model or heuristic), data source. "
            "Filter by status (running | awaiting_approval | completed | failed) or objective_id. "
            f"Start here when only an objective or a day is named. {READ_ONLY_SUFFIX}"
        ),
    )
    def list_runs(
        status: str | None = None,
        objective_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        n = max(1, min(int(limit or 20), MAX_LIST))
        return with_conn(
            lambda conn: ok(
                run_digest.list_runs_digest(
                    conn,
                    status=(status or "").strip() or None,
                    objective_id=(objective_id or "").strip() or None,
                    limit=n,
                )
            )
        )

    @mcp.tool(
        name="research.loop.get_run",
        description=(
            "One harness run, digested: the plan and where it came from (LLM hops or heuristic), "
            "the funnel cuts, the hit-rate gate, every candidate with its evidence and each judge's "
            "stance per model (agree / dissent / blocked_by_validate), the report's why / price / "
            "settled / wrong_if per name, and the drafts the run produced. Quote this record and cite "
            f"it — do not re-derive a run from live data. {READ_ONLY_SUFFIX}"
        ),
    )
    def get_run(run_id: str) -> dict[str, Any]:
        rid = (run_id or "").strip()
        if not rid:
            return err("run_id required")
        result = with_conn(lambda conn: run_digest.digest_run(conn, rid))
        if isinstance(result, dict) and result.get("ok") is False:
            return result
        if result is None:
            return err(f"run not found: {rid}")
        return ok(result)

    @mcp.tool(
        name="research.loop.explain_candidate",
        description=(
            "Why one symbol was proposed in a harness run and what would unmake the call, from the "
            "run's own record: selection evidence, price context, this source's settled record, each "
            "judge's stance by model, the report's wrong_if / falsify list, the candidate row's status, "
            "its hypothesis and any settled validation. `not_measured` means coverage, not a verdict. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def explain_candidate(run_id: str, symbol: str) -> dict[str, Any]:
        rid = (run_id or "").strip()
        sym = (symbol or "").strip().upper()
        if not rid or not sym:
            return err("run_id and symbol required")
        result = with_conn(lambda conn: run_digest.explain_candidate(conn, rid, sym))
        if isinstance(result, dict) and result.get("ok") is False:
            return result
        if result is None:
            return err(f"run not found: {rid}")
        return ok(result)
