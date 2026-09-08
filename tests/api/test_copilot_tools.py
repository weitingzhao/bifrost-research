"""The tool list is served from the registry, not from a hand-kept copy."""

from __future__ import annotations

from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app
from bifrost_research.mcp.server import TOOL_NAMES
from bifrost_research.mcp.tools._write_common import WRITE_TOOL_NAMES


def _client() -> TestClient:
    return TestClient(create_app())


def test_tools_endpoint_lists_every_registered_tool() -> None:
    with _client() as client:
        res = client.get("/research/copilot/tools")
    assert res.status_code == 200
    data = res.json()["data"]
    names = {t["name"] for t in data["tools"]}
    assert data["count"] == len(data["tools"])
    # Every canonical read tool and every write tool is present.
    assert set(TOOL_NAMES) <= names
    assert set(WRITE_TOOL_NAMES) <= names


def test_write_tools_are_marked_and_domains_are_split() -> None:
    with _client() as client:
        rows = client.get("/research/copilot/tools").json()["data"]["tools"]
    by_name = {t["name"]: t for t in rows}
    for name in WRITE_TOOL_NAMES:
        assert by_name[name]["write"] is True, name
    reads = [t for t in rows if not t["write"]]
    assert all(t["write"] is False for t in reads)
    domains = {t["domain"] for t in rows}
    assert {"research", "trade"} <= domains


def test_trade_tools_are_all_reads_and_describe_themselves() -> None:
    """D10: the Copilot may read the book and may not touch it."""
    with _client() as client:
        rows = client.get("/research/copilot/tools").json()["data"]["tools"]
    trade = [t for t in rows if t["domain"] == "trade"]
    assert len(trade) >= 10
    assert all(t["write"] is False for t in trade)
    assert all(t["description"] for t in trade)
