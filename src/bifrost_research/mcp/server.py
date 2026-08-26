"""Research MCP FastMCP server — HTTP+SSE on :8796 (D-RS-E-d / D-RS-E-h).

Read tools are always available. Write tools (RS-E4) default to dry_run=true
and require an approval token for dry_run=false.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from bifrost_research.mcp.tools import register_all
from bifrost_research.mcp.tools._write_common import WRITE_TOOL_NAMES

# Canonical READ tool names (asserted by tests). Keep in sync with tools/*.py.
TOOL_NAMES: tuple[str, ...] = (
    # hypothesis
    "research.hypothesis.list",
    "research.hypothesis.list_active",
    "research.hypothesis.get",
    "research.hypothesis.summary_active",
    # backtest
    "research.backtest.list_runs",
    "research.backtest.get_run",
    # vrp
    "research.vrp.get_latest",
    "research.vrp.get_history",
    "research.vrp.get_extremes",
    # vol_surface
    "research.vol_surface.get_fit",
    "research.vol_surface.get_term_structure",
    "research.vol_surface.get_residuals",
    "research.vol_surface.get_skew_extremes",
    # opex_cycle
    "research.opex_cycle.get_current",
    "research.opex_cycle.get_history",
    "research.opex_cycle.get_pin_analysis",
    # discovery
    "research.discovery.daily_brief_synth",
    "research.discovery.sepa_daily",
    "research.discovery.sepa_candidates",
    "research.discovery.event_radar",
    "research.discovery.momentum_radar",
    "research.discovery.forecast_sessions",
    "research.discovery.gex_intraday",
    "research.discovery.flow_sentiment",
    "research.discovery.regime_stats",
    # trade context — Wave RS-F5 (read-only Trade System bridge)
    "trade.portfolio.snapshot",
    "trade.portfolio.risk_summary",
    "trade.trading.recent_executions",
    "trade.strategy.instances",
    "trade.strategy.opportunities",
    "trade.market.watchlist",
    "trade.market.quotes",
    # playbook + copilot memory — RS-KB4
    "research.playbook.rules_active",
    "research.playbook.notes_for",
    "research.playbook.cases_matching",
    "research.playbook.recent_bridge_cases",
    "research.copilot.recent_sessions",
  # persona — RS-PS2
    "research.persona.get_effective_preferences",
)

ALL_TOOL_NAMES: tuple[str, ...] = TOOL_NAMES + WRITE_TOOL_NAMES


def create_mcp_server(
    *,
    host: str | None = None,
    port: int | None = None,
) -> FastMCP:
    """Build FastMCP app with all Research tools registered."""
    resolved_host = host or os.environ.get("HOST", "0.0.0.0")
    resolved_port = port if port is not None else int(os.environ.get("PORT", "8796"))
    mcp = FastMCP(
        name="bifrost-research",
        instructions=(
            "Bifrost Research MCP — Golden Source tools. "
            "Write tools default to dry_run=true (diff preview). "
            "Executing writes requires an approval token. "
            "D10 trade execution is blocked."
        ),
        host=resolved_host,
        port=resolved_port,
        sse_path="/sse",
        message_path="/messages/",
    )
    register_all(mcp)
    return mcp


def list_registered_tool_names(mcp: FastMCP | None = None) -> list[str]:
    server = mcp or create_mcp_server()
    tools = server._tool_manager.list_tools()  # noqa: SLF001 — test/smoke helper
    return sorted(t.name for t in tools)


def main() -> None:
    mcp = create_mcp_server()
    # D-RS-E-h: HTTP + SSE transport
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()


def __getattr__(name: str) -> Any:  # pragma: no cover
    raise AttributeError(name)
