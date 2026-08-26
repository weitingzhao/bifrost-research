"""Wave RS-F5 — trade.* MCP tools unit tests (no live network)."""

from __future__ import annotations

from typing import Any

import pytest

from bifrost_research.mcp import server as server_mod
from bifrost_research.mcp.tools import trade_context

TRADE_TOOL_NAMES = (
    "trade.portfolio.snapshot",
    "trade.portfolio.risk_summary",
    "trade.trading.recent_executions",
    "trade.strategy.instances",
    "trade.market.watchlist",
    "trade.market.quotes",
)


def test_trade_tools_in_canonical_list() -> None:
    for name in TRADE_TOOL_NAMES:
        assert name in server_mod.TOOL_NAMES


def test_trade_tools_registered() -> None:
    names = server_mod.list_registered_tool_names()
    for name in TRADE_TOOL_NAMES:
        assert name in names, f"{name} not registered on FastMCP"


def test_extract_light_status_shape() -> None:
    raw = {
        "health": {"status_lamp": "yellow"},
        "lamps": {"system_lamp": "yellow"},
        "daemon": {
            "heartbeat": {"ib_connected": True, "last_ts": 1.23},
            "trading": {
                "auto_status": {
                    "daemon_state": "running",
                    "trading_state": "BOOT",
                    "symbol": None,
                    "spot": None,
                    "daily_pnl": 0.0,
                },
                "trading_suspended": True,
            },
        },
        "portfolio": {
            "accounts": [
                {
                    "account_id": "U-1",
                    "summary": {
                        "NetLiquidation": "1000.0",
                        "GrossPositionValue": "800.0",
                        "AvailableFunds": "200.0",
                        "UNUSED": "x",
                    },
                    "positions": [{"symbol": "AAPL", "qty": 10}],
                },
                {
                    "account_id": "U-2",
                    "summary": {},
                    "positions": [],
                },
            ],
            "open_orders": [{"symbol": "MSFT"}],
            "accounts_fetched_at": 1.23,
        },
    }
    light = trade_context._extract_light_status(raw)
    assert light["daemon"]["state"] == "running"
    assert light["daemon"]["ib_connected"] is True
    assert light["daemon"]["trading_suspended"] is True
    assert light["accounts_fetched_at"] == 1.23
    assert light["open_orders"] == [{"symbol": "MSFT"}]
    assert len(light["accounts"]) == 2
    acct1 = light["accounts"][0]
    assert acct1["account_id"] == "U-1"
    assert acct1["positions_count"] == 1
    assert acct1["summary"]["NetLiquidation"] == "1000.0"
    assert "UNUSED" not in acct1["summary"], "should drop non-whitelisted summary keys"


def test_extract_light_status_handles_empty() -> None:
    light = trade_context._extract_light_status({})
    assert light["accounts"] == []
    assert light["open_orders"] == []
    assert light["daemon"]["state"] is None


def _fake_mcp() -> Any:
    """Build a fake FastMCP that records registered callables."""

    class _Fake:
        def __init__(self) -> None:
            self.tools: dict[str, Any] = {}

        def tool(self, *, name: str, description: str = "") -> Any:
            def _wrap(fn: Any) -> Any:
                self.tools[name] = fn
                return fn

            return _wrap

    return _Fake()


def test_tools_return_ok_envelope_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        trade_context,
        "get",
        lambda *_args, **_kwargs: {"executions": [{"symbol": "NVDA"}]},
    )
    fake = _fake_mcp()
    trade_context.register(fake)
    result = fake.tools["trade.trading.recent_executions"](since_hours=24)
    assert result["ok"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["executions"][0]["symbol"] == "NVDA"


def test_tools_return_err_envelope_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("trade api GET http://api/status unreachable: boom")

    monkeypatch.setattr(trade_context, "get", _boom)
    fake = _fake_mcp()
    trade_context.register(fake)
    result = fake.tools["trade.portfolio.snapshot"]()
    assert result["ok"] is False
    assert "unreachable" in result["error"]


def test_market_quotes_normalizes_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _capture(base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        seen["params"] = params or {}
        return {"quotes": [{"symbol": "AAPL"}, {"symbol": "MSFT"}]}

    monkeypatch.setattr(trade_context, "get", _capture)
    fake = _fake_mcp()
    trade_context.register(fake)
    result = fake.tools["trade.market.quotes"](symbols=" aapl , msft , , ")
    assert result["ok"] is True
    assert seen["params"]["symbols"] == "AAPL,MSFT"
    assert result["data"]["count"] == 2


def test_market_quotes_empty_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - shouldn't be called
        raise AssertionError("get() must not be called for empty symbols")

    monkeypatch.setattr(trade_context, "get", _fail)
    fake = _fake_mcp()
    trade_context.register(fake)
    result = fake.tools["trade.market.quotes"](symbols="   ")
    assert result["ok"] is True
    assert result["data"]["count"] == 0
