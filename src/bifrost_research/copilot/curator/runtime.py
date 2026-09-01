"""Headless loop_curator invocation — Wave LO-1.

Runs ``build_loop_curator_agent`` + ``Runner.run`` against a localhost MCP
instance.  Persists ``curator_trace`` on ``objective_run.outputs``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import contextmanager
from typing import Any, Generator, Protocol

from bifrost_research.copilot.curator.batch_token import issue_batch_pass
from bifrost_research.copilot.curator.mcp_local import ensure_local_mcp_url
from bifrost_research.copilot.curator.prompt import build_curator_prompt
from bifrost_research.repositories import objective as obj_repo

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("BIFROST_CURATOR_MODEL", "deepseek-chat")
DEFAULT_OWNER = os.environ.get("RESEARCH_DEFAULT_OWNER", "owner")
MAX_TURNS = int(os.environ.get("BIFROST_CURATOR_MAX_TURNS", "24"))
TIMEOUT_S = float(os.environ.get("BIFROST_CURATOR_TIMEOUT_S", "120"))


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...


@contextmanager
def _curator_batch_env(run_id: str) -> Generator[None, None, None]:
    prev = os.environ.get("BIFROST_CURATOR_RUN_ID")
    os.environ["BIFROST_CURATOR_RUN_ID"] = run_id
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("BIFROST_CURATOR_RUN_ID", None)
        else:
            os.environ["BIFROST_CURATOR_RUN_ID"] = prev


async def _run_agent_async(
    *,
    prompt: str,
    model_id: str,
    owner_id: str,
    mcp_url: str,
) -> dict[str, Any]:
    from agents import Runner
    from agents.mcp import MCPServerSse

    from bifrost_research.copilot.agents.graph import build_loop_curator_agent

    server = MCPServerSse(
        params={"url": mcp_url},
        cache_tools_list=True,
        name="research-mcp-curator",
        client_session_timeout_seconds=min(60.0, TIMEOUT_S),
    )
    agent = build_loop_curator_agent(model_id, mcp=server, owner_id=owner_id)
    async with server:
        result = await asyncio.wait_for(
            Runner.run(agent, input=prompt, max_turns=MAX_TURNS),
            timeout=TIMEOUT_S,
        )
    final = getattr(result, "final_output", None)
    return {
        "final_output": str(final) if final is not None else "",
        "model_id": model_id,
        "status": "completed",
    }


def run_curator_for_run(
    conn: _Connection,
    run_id: str,
    *,
    model_id: str | None = None,
    owner_id: str | None = None,
    skip_agent: bool = False,
) -> dict[str, Any]:
    """Execute headless CuratorRun for an objective run."""
    run = obj_repo.get_run(conn, run_id)
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    if run.get("status") not in {"awaiting_approval", "running", "completed"}:
        raise ValueError(f"run status {run.get('status')!r} not eligible for curate")

    objective = obj_repo.get_objective(conn, str(run.get("objective_id")))
    if objective is None:
        raise ValueError("objective not found for run")

    mid = model_id or DEFAULT_MODEL
    oid = owner_id or DEFAULT_OWNER
    batch_pass = issue_batch_pass(run_id)

    trace: dict[str, Any] = {
        "run_id": run_id,
        "model_id": mid,
        "owner_id": oid,
    }

    if skip_agent or os.environ.get("BIFROST_CURATOR_SKIP_AGENT", "").strip() in (
        "1",
        "true",
        "yes",
    ):
        trace["status"] = "skipped"
        trace["reason"] = "BIFROST_CURATOR_SKIP_AGENT"
        obj_repo.patch_run_outputs(conn, run_id, {"curator_trace": trace})
        return {"run_id": run_id, "curator_trace": trace}

    prompt = build_curator_prompt(run=run, objective=objective, batch_pass=batch_pass)

    try:
        with _curator_batch_env(run_id):
            mcp_url = os.environ.get("RESEARCH_MCP_SSE_URL") or ensure_local_mcp_url()
            agent_result = asyncio.run(
                _run_agent_async(prompt=prompt, model_id=mid, owner_id=oid, mcp_url=mcp_url)
            )
        trace.update(agent_result)
    except Exception as exc:
        logger.exception("CuratorRun failed for %s", run_id)
        trace["status"] = "failed"
        trace["error"] = str(exc)
        obj_repo.patch_run_outputs(conn, run_id, {"curator_trace": trace})
        return {"run_id": run_id, "curator_trace": trace, "error": str(exc)}

    outputs = dict(run.get("outputs") or {})
    prior_drafts = list(outputs.get("draft_ids") or [])
    trace["prior_draft_count"] = len(prior_drafts)
    trace["note"] = (
        "CuratorRun complete — inspect Decision Inbox for new drafts from loop_curator."
    )
    obj_repo.patch_run_outputs(conn, run_id, {"curator_trace": trace})
    return {"run_id": run_id, "curator_trace": trace}


__all__ = ["run_curator_for_run"]
