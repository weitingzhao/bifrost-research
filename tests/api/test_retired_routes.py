"""Routes retired with no caller stay retired (TD-123, modeled on trade-api TD-40).

Each path was re-checked against frontend, platform, trade-api and research MCP
before it was removed. Agent and distill triggers stayed, and launch a Dagster
job instead of running the engine in this process. Hypothesis retire stayed on
the MCP tool, which calls the repository, not this HTTP route.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from bifrost_research.api.app import create_app

API = Path(__file__).resolve().parents[2] / "src" / "bifrost_research" / "api"

RETIRED = {
    ("GET", "/analytics/sepa/technical-filter"),
    ("GET", "/analytics/sepa/screening-ranked"),
    ("GET", "/research/sepa/candidates"),
    ("GET", "/research/volatility/surface"),
    ("GET", "/research/forecast/hourly"),
    ("GET", "/research/forecast/settlement"),
    ("GET", "/research/forecast/backtest"),
    ("GET", "/research/backtest/regime-stats"),
    ("GET", "/research/canonical-pnl/coverage"),
    ("POST", "/research/forecast/terrain/compute"),
    ("POST", "/research/forecast/sessions/compute"),
    ("POST", "/research/forecast/settle"),
    ("POST", "/research/event-radar/run"),
    ("POST", "/research/events/ingest"),
    ("POST", "/research/backtest/aggregate"),
    ("POST", "/research/hypothesis/{hypothesis_id}/retire"),
}

KEPT = {
    ("GET", "/analytics/sepa/fundamental-filter"),
    ("GET", "/analytics/sepa/screener-wide"),
    ("GET", "/research/volatility/smile"),
    ("GET", "/research/sepa/daily"),
    ("GET", "/research/forecast/terrain"),
    ("GET", "/research/forecast/sessions"),
    ("GET", "/research/event-radar/events"),
    ("POST", "/research/backtest/settle"),
    ("GET", "/research/canonical-pnl/trajectory"),
    ("GET", "/research/canonical-pnl/structures"),
    ("POST", "/research/agents/morning/run"),
    ("POST", "/research/agents/digest/run"),
    ("POST", "/research/agents/weekly-policy/run"),
    ("POST", "/research/agents/eod/run"),
    ("POST", "/research/journal/memory/distill"),
    ("POST", "/research/hypothesis/{hypothesis_id}/refresh-trajectory"),
}

# Names the deleted triggers imported. A route that grows one of these back
# is running engine code in the API process again.
_FORBIDDEN_IMPORTS = {
    ("bifrost_research.engines.journal_distill", "run_distill"),
    ("bifrost_research.engines.event_radar.pipeline", "run_pipeline"),
    ("bifrost_research.engines.forecast.terrain", "compute_market_terrain"),
    ("bifrost_research.engines.forecast.playbook", "build_forecast_session"),
    ("bifrost_research.engines.forecast.llm", "get_default_provider"),
    ("bifrost_research.engines.backtest.regime_stats", "compute_regime_stats"),
    ("bifrost_research.copilot.agents.daily_digest", "run_daily_digest"),
    ("bifrost_research.copilot.agents.weekly_policy_review", "run_weekly_policy_review"),
}


_HTTP = {"get", "post", "put", "patch", "delete"}


def _served(app: Any) -> set[tuple[str, str]]:
    """OpenAPI, not app.routes: the metrics middleware hides the mounted routes."""
    found: set[tuple[str, str]] = set()
    for path, item in app.openapi()["paths"].items():
        for method in item:
            if method in _HTTP:
                found.add((method.upper(), path))
    return found


def test_retired_routes_are_gone() -> None:
    assert not sorted(RETIRED & _served(create_app()))


def test_their_neighbours_are_served() -> None:
    assert not sorted(KEPT - _served(create_app()))


def test_api_does_not_import_retired_engine_entries() -> None:
    hits: list[str] = []
    for path in sorted(API.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            for alias in node.names:
                if (node.module, alias.name) in _FORBIDDEN_IMPORTS:
                    hits.append(f"{path.name}:{node.lineno} {node.module}.{alias.name}")
    assert hits == []
