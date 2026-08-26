"""Wave RS-E4.1 — write MCP tools (dry_run + approval gate)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bifrost_research.copilot.approvals import issue_token, reset_consumed_for_tests
from bifrost_research.mcp.server import ALL_TOOL_NAMES, create_mcp_server
from bifrost_research.mcp.tools._write_common import WRITE_TOOL_NAMES


@pytest.fixture(autouse=True)
def _reset_tokens() -> None:
    reset_consumed_for_tests()
    yield
    reset_consumed_for_tests()


def _call_sync(mcp: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke a FastMCP tool synchronously via the tool manager."""
    tool = mcp._tool_manager.get_tool(name)  # noqa: SLF001
    assert tool is not None, f"missing tool {name}"
    result = tool.fn(**arguments)
    assert isinstance(result, dict)
    return result


def test_write_tools_registered() -> None:
    registered = {t.name for t in create_mcp_server()._tool_manager.list_tools()}  # noqa: SLF001
    for name in WRITE_TOOL_NAMES:
        assert name in registered
    assert set(WRITE_TOOL_NAMES).issubset(set(ALL_TOOL_NAMES))


def test_create_hypothesis_dry_run_preview() -> None:
    mcp = create_mcp_server()
    result = _call_sync(
        mcp,
        "research.hypothesis.create",
        {
            "title": "NVDA vol crush",
            "thesis": "IV crush post-earnings",
            "symbols": ["NVDA"],
            "dry_run": True,
        },
    )
    assert result["ok"] is True
    data = result["data"]
    assert data["dry_run"] is True
    assert data["diff_kind"] == "create_hypothesis"
    assert data["preview"]["title"] == "NVDA vol crush"
    assert data["impact"]["table"] == "research.hypothesis"
    assert data["impact"]["creates_row"] is True


def test_patch_and_retire_dry_run() -> None:
    mcp = create_mcp_server()
    patch = _call_sync(
        mcp,
        "research.hypothesis.patch",
        {
            "hypothesis_id": "hyp-1",
            "status": "validated",
            "dry_run": True,
        },
    )
    assert patch["ok"] is True
    assert patch["data"]["diff_kind"] == "patch_hypothesis"
    assert patch["data"]["preview"]["fields"]["status"] == "validated"

    retire = _call_sync(
        mcp,
        "research.hypothesis.retire",
        {"hypothesis_id": "hyp-1", "dry_run": True},
    )
    assert retire["ok"] is True
    assert retire["data"]["diff_kind"] == "retire_hypothesis"


def test_backtest_dry_run_preview() -> None:
    mcp = create_mcp_server()
    result = _call_sync(
        mcp,
        "research.backtest.run_event_query",
        {
            "strategy_template": "long_atm_straddle",
            "event_kind": "earnings",
            "lookback_years": 3,
            "dry_run": True,
        },
    )
    assert result["ok"] is True
    data = result["data"]
    assert data["diff_kind"] == "run_backtest"
    assert data["dry_run"] is True
    assert data["preview"]["strategy_template"] == "long_atm_straddle"
    assert data["preview"]["template_known"] is True


def test_execute_without_token_rejected() -> None:
    mcp = create_mcp_server()
    result = _call_sync(
        mcp,
        "research.hypothesis.create",
        {
            "title": "x",
            "thesis": "y",
            "dry_run": False,
        },
    )
    assert result["ok"] is False
    assert "403" in result["error"]
    assert result.get("status") == 403


def test_execute_with_valid_token_calls_repo() -> None:
    mcp = create_mcp_server()
    args = {
        "title": "NVDA vol crush",
        "thesis": "post-earn IV crush",
        "symbols": ["NVDA"],
        "tags": [],
        "status": "active",
        "origin_page": "copilot",
        "conclusion": None,
    }
    issued = issue_token(
        action_id="aal_test_create",
        tool="research.hypothesis.create",
        arguments=args,
    )
    fake_row = {
        "id": "nvda-vol-crush-abc",
        "title": args["title"],
        "thesis": args["thesis"],
        "symbols": ["NVDA"],
    }
    with patch(
        "bifrost_research.mcp.tools.write_hypothesis.repo.create_hypothesis",
        return_value=fake_row,
    ) as mock_create:
        with patch(
            "bifrost_research.mcp.tools.write_hypothesis.with_conn",
            side_effect=lambda fn: fn(MagicMock()),
        ):
            result = _call_sync(
                mcp,
                "research.hypothesis.create",
                {**args, "dry_run": False, "approval_token": issued["approval_token"]},
            )
    assert result["ok"] is True
    assert result["data"]["executed"] is True
    assert result["data"]["result"]["id"] == "nvda-vol-crush-abc"
    mock_create.assert_called_once()


def test_d10_no_trade_symbols_in_write_modules() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "bifrost_research"
    forbidden = ("place_order", "ib:operator", "ib:operator:cmd", "POST /control/")
    offenders: list[str] = []
    for sub in ("mcp", "copilot"):
        for path in (root / sub).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{path.name}:{needle}")
    assert offenders == [], offenders
