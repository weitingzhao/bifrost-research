"""MCP tools: trade.* — Trade System context for Research Copilot (Wave RS-F5).

Read-only bridge tools that let the Copilot combine live portfolio + market
snapshot from the Trade cluster (bifrost-prod) with Research analytics from
Golden Source. All tools are GET-only wrappers over bifrost-trade-api.

D10 (live trading) is enforced upstream: Trade APIs expose only read routes
here; there is no code path that can arm the daemon or issue IB operator
commands from these tools. Order-placement paths are blocked by policy.
"""

from __future__ import annotations

import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.mcp.tools._common import READ_ONLY_SUFFIX, err, ok
from bifrost_research.mcp.tools._trade_api_client import (
    base_market,
    base_monitor,
    base_strategy,
    base_trading,
    get,
)


def _safe(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return ok(fn(*args, **kwargs))
    except RuntimeError as exc:
        return err(str(exc))
    except Exception as exc:  # noqa: BLE001
        return err(f"{type(exc).__name__}: {exc}")


def _extract_light_status(status: dict[str, Any]) -> dict[str, Any]:
    """Pare down /status to portfolio + daemon summary; drop verbose config."""
    accounts = status.get("portfolio", {}).get("accounts") or []
    trimmed_accounts: list[dict[str, Any]] = []
    for acct in accounts:
        summary = acct.get("summary") or {}
        summary_keys = (
            "NetLiquidation",
            "TotalCashValue",
            "BuyingPower",
            "AvailableFunds",
            "ExcessLiquidity",
            "GrossPositionValue",
            "MaintMarginReq",
            "UnrealizedPnL",
            "RealizedPnL",
            "AccountType",
            "Currency",
        )
        trimmed_accounts.append(
            {
                "account_id": acct.get("account_id"),
                "summary": {k: summary.get(k) for k in summary_keys if k in summary},
                "positions": acct.get("positions") or [],
                "positions_count": len(acct.get("positions") or []),
            }
        )
    daemon = status.get("daemon", {}) or {}
    hb = daemon.get("heartbeat") or {}
    return {
        "health": status.get("health"),
        "lamps": status.get("lamps"),
        "daemon": {
            "state": (daemon.get("trading") or {}).get("auto_status", {}).get("daemon_state"),
            "trading_state": (daemon.get("trading") or {})
            .get("auto_status", {})
            .get("trading_state"),
            "trading_suspended": (daemon.get("trading") or {}).get("trading_suspended"),
            "symbol": (daemon.get("trading") or {}).get("auto_status", {}).get("symbol"),
            "spot": (daemon.get("trading") or {}).get("auto_status", {}).get("spot"),
            "daily_pnl": (daemon.get("trading") or {}).get("auto_status", {}).get("daily_pnl"),
            "ib_connected": hb.get("ib_connected"),
            "heartbeat_ts": hb.get("last_ts"),
        },
        "accounts": trimmed_accounts,
        "open_orders": (status.get("portfolio", {}) or {}).get("open_orders") or [],
        "accounts_fetched_at": (status.get("portfolio", {}) or {}).get("accounts_fetched_at"),
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="trade.portfolio.snapshot",
        description=(
            "Current portfolio snapshot from Bifrost Trade — accounts, positions, "
            "open orders, and daemon state. Use this to answer questions like "
            "'what am I holding right now?' or to combine holdings with market "
            "conditions when making recommendations. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def portfolio_snapshot() -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            status = get(base_monitor(), "/status")
            return _extract_light_status(status if isinstance(status, dict) else {})

        return _safe(_run)

    @mcp.tool(
        name="trade.portfolio.risk_summary",
        description=(
            "Compact risk summary: spot, symbol, daily P&L, daily hedge count, "
            "operations count. Cheaper than snapshot for quick health checks. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def portfolio_risk_summary() -> dict[str, Any]:
        return _safe(lambda: get(base_monitor(), "/risk_summary"))

    @mcp.tool(
        name="trade.trading.recent_executions",
        description=(
            "Recent executions (trades) across accounts. Default lookback 7 days. "
            "Set `since_hours` to widen/narrow the window. Optional `account_id`. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def trading_recent_executions(
        since_hours: int = 168,
        account_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            since_ts = max(0.0, time.time() - max(1, int(since_hours)) * 3600)
            params: dict[str, Any] = {"since_ts": since_ts}
            if account_id:
                params["account_id"] = account_id
            data = get(base_trading(), "/executions", params=params)
            rows = (data or {}).get("executions") or []
            trimmed = rows[: max(1, min(int(limit), 500))]
            return {
                "executions": trimmed,
                "count": len(trimmed),
                "returned_from": len(rows),
                "since_ts": since_ts,
            }

        return _safe(_run)

    @mcp.tool(
        name="trade.strategy.instances",
        description=(
            "Active strategy instances (running / paused / recently completed). "
            "Answers 'which strategies is the daemon currently running?'. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def strategy_instances(limit: int = 50) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            data = get(base_strategy(), "/instances")
            rows = (data or {}).get("instances") if isinstance(data, dict) else None
            if rows is None and isinstance(data, list):
                rows = data
            rows = rows or []
            trimmed = rows[: max(1, min(int(limit), 200))]
            return {"instances": trimmed, "count": len(trimmed)}

        return _safe(_run)

    @mcp.tool(
        name="trade.market.watchlist",
        description=(
            "Current watchlist items (symbols, sec type, category). "
            "Use to know which symbols the user is actively tracking. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def market_watchlist() -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            data = get(base_market(), "/watchlist")
            items = (data or {}).get("items") if isinstance(data, dict) else None
            if items is None and isinstance(data, list):
                items = data
            items = items or []
            return {"items": items, "count": len(items)}

        return _safe(_run)

    @mcp.tool(
        name="trade.market.quotes",
        description=(
            "Real-time quotes for one or more symbols. `symbols` is a comma-"
            "separated list (max 20). Combine with portfolio.snapshot for "
            "'given my positions + current market, what should I do?' answers. "
            f"{READ_ONLY_SUFFIX}"
        ),
    )
    def market_quotes(symbols: str) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            cleaned = ",".join(
                sym.strip().upper() for sym in (symbols or "").split(",") if sym.strip()
            )[:1000]
            if not cleaned:
                return {"quotes": [], "count": 0}
            data = get(base_market(), "/quotes", params={"symbols": cleaned})
            quotes = (data or {}).get("quotes") if isinstance(data, dict) else None
            if quotes is None and isinstance(data, list):
                quotes = data
            quotes = quotes or []
            return {"quotes": quotes, "count": len(quotes), "symbols": cleaned}

        return _safe(_run)
