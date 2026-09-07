"""research-loop-automation D1 — research.loop.* read tools and run_objective."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from bifrost_research.copilot.approvals import issue_token, reset_consumed_for_tests
from bifrost_research.copilot.harness import run_digest
from bifrost_research.mcp.server import ALL_TOOL_NAMES, TOOL_NAMES, create_mcp_server
from bifrost_research.mcp.tools import loop as loop_tools
from bifrost_research.mcp.tools import write_loop


@pytest.fixture(autouse=True)
def _reset_tokens() -> None:
    reset_consumed_for_tests()
    yield
    reset_consumed_for_tests()


class _Conn:
    def close(self) -> None:
        pass


def _tool(name: str) -> Any:
    mcp = create_mcp_server()
    tool = mcp._tool_manager.get_tool(name)  # noqa: SLF001
    assert tool is not None, name
    return tool


def test_loop_tools_registered_and_described() -> None:
    for name in ("research.loop.list_runs", "research.loop.get_run", "research.loop.explain_candidate"):
        assert name in TOOL_NAMES
        assert "Does not modify data" in (_tool(name).description or "")
    assert "research.loop.run_objective" in ALL_TOOL_NAMES
    assert "Write tool" in (_tool("research.loop.run_objective").description or "")


def test_get_run_and_explain_quote_the_digest(monkeypatch) -> None:
    digest = {"run": {"id": "run_1", "status": "awaiting_approval"}, "candidates": [{"symbol": "WT"}], "advisory": run_digest.ADVISORY}
    monkeypatch.setattr(loop_tools, "with_conn", lambda fn: fn(_Conn()))
    monkeypatch.setattr(run_digest, "digest_run", lambda conn, rid: digest if rid == "run_1" else None)
    monkeypatch.setattr(
        run_digest,
        "explain_candidate",
        lambda conn, rid, sym: {"found": True, "symbol": sym, "what_would_unmake_it": ["close below the 50-day"]} if rid == "run_1" else None,
    )
    monkeypatch.setattr(run_digest, "list_runs_digest", lambda conn, **kw: {"items": [{"id": "run_1"}], "count": 1, "kw": kw})

    got = _tool("research.loop.get_run").fn(run_id="run_1")
    assert got["ok"] is True and got["data"]["candidates"] == [{"symbol": "WT"}]
    assert _tool("research.loop.get_run").fn(run_id="run_404")["ok"] is False
    assert _tool("research.loop.get_run").fn(run_id="")["ok"] is False

    why = _tool("research.loop.explain_candidate").fn(run_id="run_1", symbol="wt")
    assert why["ok"] is True and why["data"]["symbol"] == "WT" and why["data"]["what_would_unmake_it"] == ["close below the 50-day"]
    assert _tool("research.loop.explain_candidate").fn(run_id="run_1", symbol="")["ok"] is False

    listed = _tool("research.loop.list_runs").fn(status="awaiting_approval", limit=500)
    assert listed["ok"] is True and listed["data"]["count"] == 1
    assert listed["data"]["kw"] == {"status": "awaiting_approval", "objective_id": None, "limit": loop_tools.MAX_LIST}


def test_read_tools_report_db_failure_as_error(monkeypatch) -> None:
    with patch("bifrost_research.mcp.tools._common.connect", side_effect=RuntimeError("db down")):
        out = _tool("research.loop.get_run").fn(run_id="run_1")
    assert out["ok"] is False and "db down" in out["error"]


OBJ = {"id": "obj_stock", "title": "Daily Loop Stock Explorer", "status": "active", "policy_json": {"universe_mode": "stock_composite", "max_candidates": 8, "persona_evaluate": True}}


def test_run_objective_dry_run_previews_without_creating_a_run(monkeypatch) -> None:
    monkeypatch.setattr(write_loop, "with_conn", lambda fn: fn(_Conn()))
    monkeypatch.setattr(write_loop.obj_repo, "get_objective", lambda conn, oid: OBJ if oid == "obj_stock" else None)
    import bifrost_research.copilot.harness.batch_orchestrate as bo

    monkeypatch.setattr(bo, "trust_status", lambda: {"level": "L1", "auto_approve": False})
    started: list[Any] = []
    monkeypatch.setattr(bo, "start_async_batch", lambda conn, obj, *, curate_after: started.append(obj) or {"run": {"id": "run_new"}, "started": True})

    out = _tool("research.loop.run_objective").fn(objective_id="obj_stock", dry_run=True)
    assert out["ok"] is True and out["data"]["dry_run"] is True and out["data"]["diff_kind"] == "loop_run"
    preview = out["data"]["preview"]
    assert preview["objective"]["title"] == "Daily Loop Stock Explorer"
    assert preview["objective"]["max_candidates"] == 8 and preview["objective"]["persona_evaluate"] is True
    assert preview["trust"] == {"level": "L1", "auto_approve": False}
    assert isinstance(preview["plan"]["steps"], list) and preview["curate_after"] is True
    assert out["data"]["impact"]["table"] == "research.objective_run"
    assert started == []

    assert _tool("research.loop.run_objective").fn(objective_id="obj_404", dry_run=True)["ok"] is False
    monkeypatch.setattr(write_loop.obj_repo, "get_objective", lambda conn, oid: {**OBJ, "status": "archived"})
    assert "not active" in _tool("research.loop.run_objective").fn(objective_id="obj_stock", dry_run=True)["error"]


def test_run_objective_execute_needs_an_owner_token_never_a_batch_pass(monkeypatch) -> None:
    monkeypatch.setattr(write_loop, "with_conn", lambda fn: fn(_Conn()))
    monkeypatch.setattr(write_loop.obj_repo, "get_objective", lambda conn, oid: OBJ)
    import bifrost_research.copilot.harness.batch_orchestrate as bo

    started: list[Any] = []
    monkeypatch.setattr(bo, "start_async_batch", lambda conn, obj, *, curate_after: started.append(curate_after) or {"run": {"id": "run_new"}, "started": True})

    no_token = _tool("research.loop.run_objective").fn(objective_id="obj_stock", dry_run=False)
    assert no_token["ok"] is False and started == []

    monkeypatch.setattr(write_loop, "looks_like_batch_pass", lambda token: token.startswith("bp_"))
    batch = _tool("research.loop.run_objective").fn(objective_id="obj_stock", dry_run=False, approval_token="bp_curator")
    assert batch["ok"] is False and batch.get("status") == 403 and "batch pass" in batch["error"] and started == []

    issued = issue_token(
        action_id="act_1",
        tool="research.loop.run_objective",
        arguments={"objective_id": "obj_stock", "curate_after": False},
    )
    done = _tool("research.loop.run_objective").fn(
        objective_id="obj_stock", curate_after=False, dry_run=False, approval_token=issued["approval_token"]
    )
    assert done["ok"] is True and done["data"]["executed"] is True
    assert done["data"]["result"]["run"]["id"] == "run_new" and started == [False]
